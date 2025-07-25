# -*- coding: UTF-8 -*-
import datetime
import logging
import re
import time
import traceback

import simplejson as json
from django.contrib.auth.decorators import permission_required
from django.db import connection, close_old_connections
from django.db.models import Q
from django.http import HttpResponse
from common.config import SysConfig
from common.utils.extend_json_encoder import ExtendJSONEncoder, ExtendJSONEncoderFTime
from common.utils.openai import OpenaiClient, check_openai_config
from common.utils.timer import FuncTimer
from sql.query_privileges import query_priv_check
from sql.utils.resource_group import user_instances
from sql.utils.tasks import add_kill_conn_schedule, del_schedule
from .models import QueryLog, Instance
from sql.utils.download_help import replace_limit,generate_insert_sql
from sql.engines import get_engine

logger = logging.getLogger("default")


@permission_required("sql.query_submit", raise_exception=True)
def query(request):
    """
    获取SQL查询结果
    :param request:
    :return:
    """
    instance_name = request.POST.get("instance_name")
    sql_content = request.POST.get("sql_content")
    db_name = request.POST.get("db_name")
    tb_name = request.POST.get("tb_name")
    limit_num = int(request.POST.get("limit_num", 0))
    schema_name = request.POST.get("schema_name", None)
    user = request.user

    result = {"status": 0, "msg": "ok", "data": {}}
    try:
        instance = user_instances(request.user).get(instance_name=instance_name)
    except Instance.DoesNotExist:
        result["status"] = 1
        result["msg"] = "你所在组未关联该实例"
        return HttpResponse(json.dumps(result), content_type="application/json")

    # 服务器端参数验证
    if None in [sql_content, db_name, instance_name, limit_num]:
        result["status"] = 1
        result["msg"] = "页面提交参数可能为空"
        return HttpResponse(json.dumps(result), content_type="application/json")

    try:
        config = SysConfig()
        # 查询前的检查，禁用语句检查，语句切分
        query_engine = get_engine(instance=instance)
        query_check_info = query_engine.query_check(db_name=db_name, sql=sql_content)
        if query_check_info.get("bad_query"):
            # 引擎内部判断为 bad_query
            result["status"] = 1
            result["msg"] = query_check_info.get("msg")
            return HttpResponse(json.dumps(result), content_type="application/json")
        if query_check_info.get("has_star") and config.get("disable_star") is True:
            # 引擎内部判断为有 * 且禁止 * 选项打开
            result["status"] = 1
            result["msg"] = query_check_info.get("msg")
            return HttpResponse(json.dumps(result), content_type="application/json")
        sql_content = query_check_info["filtered_sql"]

        # 查询权限校验，并且获取limit_num
        priv_check_info = query_priv_check(
            user, instance, db_name, sql_content, limit_num
        )
        if priv_check_info["status"] == 0:
            limit_num = priv_check_info["data"]["limit_num"]
            priv_check = priv_check_info["data"]["priv_check"]
        else:
            result["status"] = priv_check_info["status"]
            result["msg"] = priv_check_info["msg"]
            return HttpResponse(json.dumps(result), content_type="application/json")
        # explain的limit_num设置为0
        limit_num = 0 if re.match(r"^explain", sql_content.lower()) else limit_num

        # 对查询sql增加limit限制或者改写语句
        sql_content = query_engine.filter_sql(sql=sql_content, limit_num=limit_num)

        # 先获取查询连接，用于后面查询复用连接以及终止会话
        query_engine.get_connection(db_name=db_name)
        thread_id = query_engine.thread_id
        max_execution_time = int(config.get("max_execution_time", 60))
        # 执行查询语句，并增加一个定时终止语句的schedule，timeout=max_execution_time
        if thread_id:
            schedule_name = f"query-{time.time()}"
            run_date = datetime.datetime.now() + datetime.timedelta(
                seconds=max_execution_time
            )
            add_kill_conn_schedule(schedule_name, run_date, instance.id, thread_id)
        with FuncTimer() as t:
            # 获取主从延迟信息
            seconds_behind_master = query_engine.seconds_behind_master
            query_result = query_engine.query(
                db_name,
                sql_content,
                limit_num,
                schema_name=schema_name,
                tb_name=tb_name,
                max_execution_time=max_execution_time * 1000,
            )
        query_result.query_time = t.cost
        # 返回查询结果后删除schedule
        if thread_id:
            del_schedule(schedule_name)

        # 查询异常
        if query_result.error:
            result["status"] = 1
            result["msg"] = query_result.error
        # 数据脱敏，仅对查询无错误的结果集进行脱敏，并且按照query_check配置是否返回
        elif config.get("data_masking"):
            try:
                with FuncTimer() as t:
                    masking_result = query_engine.query_masking(
                        db_name, sql_content, query_result
                    )
                masking_result.mask_time = t.cost
                # 脱敏出错
                if masking_result.error:
                    # 开启query_check，直接返回异常，禁止执行
                    if config.get("query_check"):
                        result["status"] = 1
                        result["msg"] = f"数据脱敏异常：{masking_result.error}"
                    # 关闭query_check，忽略错误信息，返回未脱敏数据，权限校验标记为跳过
                    else:
                        logger.warning(
                            f"数据脱敏异常，按照配置放行，查询语句：{sql_content}，错误信息：{masking_result.error}"
                        )
                        query_result.error = None
                        result["data"] = query_result.__dict__
                # 正常脱敏
                else:
                    result["data"] = masking_result.__dict__
            except Exception as msg:
                logger.error(traceback.format_exc())
                # 抛出未定义异常，并且开启query_check，直接返回异常，禁止执行
                if config.get("query_check"):
                    result["status"] = 1
                    result["msg"] = f"数据脱敏异常，请联系管理员，错误信息：{msg}"
                # 关闭query_check，忽略错误信息，返回未脱敏数据，权限校验标记为跳过
                else:
                    logger.warning(
                        f"数据脱敏异常，按照配置放行，查询语句：{sql_content}，错误信息：{msg}"
                    )
                    query_result.error = None
                    result["data"] = query_result.__dict__
        # 无需脱敏的语句
        else:
            result["data"] = query_result.__dict__

        # 仅将成功的查询语句记录存入数据库
        if not query_result.error:
            result["data"]["seconds_behind_master"] = seconds_behind_master
            if int(limit_num) == 0:
                limit_num = int(query_result.affected_rows)
            else:
                limit_num = min(int(limit_num), int(query_result.affected_rows))
            # 防止查询超时
            if connection.connection and not connection.is_usable():
                close_old_connections()
        else:
            limit_num = 0
        query_log = QueryLog(
            username=user.username,
            user_display=user.display,
            db_name=db_name,
            instance_name=instance.instance_name,
            sqllog=sql_content,
            effect_row=limit_num,
            cost_time=query_result.query_time,
            priv_check=priv_check,
            hit_rule=query_result.mask_rule_hit,
            masking=query_result.is_masked,
            query_type="online_query",
        )
        query_log.save()
    except Exception as e:
        logger.error(
            f"查询异常报错，查询语句：{sql_content}\n，错误信息：{traceback.format_exc()}"
        )
        result["status"] = 1
        result["msg"] = f"查询异常报错，错误信息：{e}"
        return HttpResponse(json.dumps(result), content_type="application/json")
    # 返回查询结果
    try:
        return HttpResponse(
            json.dumps(
                result,
                use_decimal=False,
                cls=ExtendJSONEncoderFTime,
                bigint_as_string=True,
            ),
            content_type="application/json",
        )
    # 虽然能正常返回，但是依然会乱码
    except UnicodeDecodeError:
        return HttpResponse(
            json.dumps(result, default=str, bigint_as_string=True, encoding="latin1"),
            content_type="application/json",
        )

@permission_required("sql.query_submit", raise_exception=True)
def query_download(request):
    """
    获取SQL查询结果
    :param request:
    :return:
    """
    request_data = json.loads(request.body)

    instance_name = request_data.get("instance_name")
    sql_content = request_data.get("sql_content")
    db_name = request_data.get("db_name")
    tb_name = request_data.get("tb_name")
    limit_num = int(request_data.get("limit_num", 0))
    download_type = request_data.get("download_type", "excel")

    schema_name = request.POST.get("schema_name", None)
    user = request.user

    result = {"status": 0, "msg": "ok", "data": {}}
    try:
        instance = user_instances(request.user).get(instance_name=instance_name)
    except Instance.DoesNotExist:
        result["status"] = 1
        result["msg"] = "你所在组未关联该实例"
        return HttpResponse(json.dumps(result), content_type="application/json")
    # 服务器端参数验证
    if None in [sql_content, db_name, instance_name, limit_num]:
        result["status"] = 1
        result["msg"] = "页面提交参数可能为空"
        return HttpResponse(json.dumps(result), content_type="application/json")

    try:
        config = SysConfig()
        # 查询前的检查，禁用语句检查，语句切分
        query_engine = get_engine(instance=instance)

        query_check_info = query_engine.query_check(db_name=db_name, sql=sql_content)
        if query_check_info.get("bad_query"):

            # 引擎内部判断为 bad_query
            result["status"] = 1
            result["msg"] = query_check_info.get("msg")
            return HttpResponse(json.dumps(result), content_type="application/json")
        if query_check_info.get("has_star") and config.get("disable_star") is True:
            # 引擎内部判断为有 * 且禁止 * 选项打开
            result["status"] = 1
            result["msg"] = query_check_info.get("msg")
            return HttpResponse(json.dumps(result), content_type="application/json")
        sql_content = query_check_info["filtered_sql"]

        # 查询权限校验，并且获取limit_num
        priv_check_info = query_priv_check(
            user, instance, db_name, sql_content, limit_num
        )
        if priv_check_info["status"] == 0:
            limit_num = priv_check_info["data"]["limit_num"]
            priv_check = priv_check_info["data"]["priv_check"]
        else:
            result["status"] = priv_check_info["status"]
            result["msg"] = priv_check_info["msg"]
            return HttpResponse(json.dumps(result), content_type="application/json")

        # explain的limit_num设置为0
        limit_num = 0 if re.match(r"^explain", sql_content.lower()) else limit_num
        # 对查询sql增加limit限制或者改写语句
        sql_content = query_engine.filter_sql(sql=sql_content, limit_num=limit_num)
        limit_num=config.get("download_limit")


        sql_content=replace_limit(sql_content,limit_num)
        # 先获取查询连接，用于后面查询复用连接以及终止会话
        query_engine.get_connection(db_name=db_name)
        thread_id = query_engine.thread_id
        max_execution_time = int(config.get("max_execution_time", 60))
        # 执行查询语句，并增加一个定时终止语句的schedule，timeout=max_execution_time
        if thread_id:
            schedule_name = f"query-{time.time()}"
            run_date = datetime.datetime.now() + datetime.timedelta(
                seconds=max_execution_time
            )
            add_kill_conn_schedule(schedule_name, run_date, instance.id, thread_id)
        with FuncTimer() as t:
            # 获取主从延迟信息
            seconds_behind_master = query_engine.seconds_behind_master
            query_result = query_engine.query(
                db_name,
                sql_content,
                limit_num,
                schema_name=schema_name,
                tb_name=tb_name,
                max_execution_time=max_execution_time * 1000,
            )
        query_result.query_time = t.cost
        # 返回查询结果后删除schedule
        if thread_id:
            del_schedule(schedule_name)

        # 查询异常
        if query_result.error:
            result["status"] = 1
            result["msg"] = query_result.error
        # 数据脱敏，仅对查询无错误的结果集进行脱敏，并且按照query_check配置是否返回
        elif config.get("data_masking"):
            try:
                with FuncTimer() as t:
                    masking_result = query_engine.query_masking(
                        db_name, sql_content, query_result
                    )
                masking_result.mask_time = t.cost
                # 脱敏出错
                if masking_result.error:
                    # 开启query_check，直接返回异常，禁止执行
                    if config.get("query_check"):
                        result["status"] = 1
                        result["msg"] = f"数据脱敏异常：{masking_result.error}"
                    # 关闭query_check，忽略错误信息，返回未脱敏数据，权限校验标记为跳过
                    else:
                        logger.warning(
                            f"数据脱敏异常，按照配置放行，查询语句：{sql_content}，错误信息：{masking_result.error}"
                        )
                        query_result.error = None
                        result["data"] = query_result.__dict__
                # 正常脱敏
                else:
                    result["data"] = masking_result.__dict__
            except Exception as msg:
                logger.error(traceback.format_exc())
                # 抛出未定义异常，并且开启query_check，直接返回异常，禁止执行
                if config.get("query_check"):
                    result["status"] = 1
                    result["msg"] = f"数据脱敏异常，请联系管理员，错误信息：{msg}"
                # 关闭query_check，忽略错误信息，返回未脱敏数据，权限校验标记为跳过
                else:
                    logger.warning(
                        f"数据脱敏异常，按照配置放行，查询语句：{sql_content}，错误信息：{msg}"
                    )
                    query_result.error = None
                    result["data"] = query_result.__dict__
        # 无需脱敏的语句
        else:
            result["data"] = query_result.__dict__

        # 仅将成功的查询语句记录存入数据库
        if not query_result.error:
            result["data"]["seconds_behind_master"] = seconds_behind_master
            if int(limit_num) == 0:
                limit_num = int(query_result.affected_rows)
            else:
                limit_num = min(int(limit_num), int(query_result.affected_rows))
            # 防止查询超时
            if connection.connection and not connection.is_usable():
                close_old_connections()
        else:
            limit_num = 0
        query_log = QueryLog(
            username=user.username,
            user_display=user.display,
            db_name=db_name,
            instance_name=instance.instance_name,
            sqllog=sql_content,
            effect_row=limit_num,
            cost_time=query_result.query_time,
            priv_check=priv_check,
            hit_rule=query_result.mask_rule_hit,
            masking=query_result.is_masked,
            query_type="download_query"
        )
        query_log.save()
    except Exception as e:
        logger.error(
            f"查询异常报错，查询语句：{sql_content}\n，错误信息：{traceback.format_exc()}"
        )
        result["status"] = 1
        result["msg"] = f"查询异常报错，错误信息：{e}"
        return HttpResponse(json.dumps(result), content_type="application/json")
    # 返回查询结果

    if download_type=='excel':
        # wb = Workbook()
        # ws = wb.active
        # ws.title = "Sheet1"
        #
        # # 写入表头
        # ws.append(result["data"]["column_list"])
        # from io import BytesIO
        # # # 写入数据行
        # for row in result["data"]["rows"]:
        #     ws.append(row)  # 根据 headers 顺序填充
        # output = BytesIO()
        # wb.save(output)
        # output.seek(0)
        import xlsxwriter
        from io import BytesIO
        output = BytesIO()
        workbook = xlsxwriter.Workbook(output, {'in_memory': True})
        worksheet = workbook.add_worksheet("Sheet1")
        # 写入表头
        headers = result["data"]["column_list"]
        for col_num, header in enumerate(headers):
            worksheet.write(0, col_num, header)
        # 写入数据
        for row_num, row in enumerate(result["data"]["rows"], start=1):
            for col_num, cell in enumerate(row):
                worksheet.write(row_num, col_num, cell)
        workbook.close()
        output.seek(0)
        # 构造响应
        response = HttpResponse(
            output,
            content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
        )
        response['Content-Disposition'] = 'attachment; filename=导出数据.xlsx'
        return response
    else:
        download_data=generate_insert_sql('table_name', result["data"]["column_list"], result["data"]["column_type"], result["data"]["rows"], batch_size=10)

        file_name = 'filtered_data.txt'
        with open(file_name, 'w', encoding='utf-8') as file:
            for line in download_data:
                file.write(line)

                # 读取文件内容并返回给前端
        with open(file_name, 'r', encoding='utf-8') as file:
            file_content = file.read()

        # 删除生成的txt文件
        import os
        os.remove(file_name)

        # 返回文本文件内容给前端
        return HttpResponse(file_content, content_type='text/plain', status=200)



@permission_required("sql.menu_sqlquery", raise_exception=True)
def querylog(request):
    return _querylog(request)


@permission_required("sql.audit_user", raise_exception=True)
def querylog_audit(request):
    return _querylog(request)


def _querylog(request):
    """
    获取sql查询记录
    :param request:
    :return:
    """
    # 获取用户信息
    user = request.user

    limit = int(request.GET.get("limit", 0))
    offset = int(request.GET.get("offset", 0))
    limit = offset + limit
    limit = limit if limit else None
    star = True if request.GET.get("star") == "true" else False
    query_log_id = request.GET.get("query_log_id")
    search = request.GET.get("search", "")
    start_date = request.GET.get("start_date", "")
    end_date = request.GET.get("end_date", "")

    # 组合筛选项
    filter_dict = dict()
    # 是否收藏
    if star:
        filter_dict["favorite"] = star
    # 语句别名
    if query_log_id:
        filter_dict["id"] = query_log_id

    # 管理员、审计员查看全部数据,普通用户查看自己的数据
    if not (user.is_superuser or user.has_perm("sql.audit_user")):
        filter_dict["username"] = user.username

    if start_date and end_date:
        end_date = datetime.datetime.strptime(
            end_date, "%Y-%m-%d"
        ) + datetime.timedelta(days=1)
        filter_dict["create_time__range"] = (start_date, end_date)

    # 过滤组合筛选项
    sql_log = QueryLog.objects.filter(**filter_dict)

    # 过滤搜索信息
    sql_log = sql_log.filter(
        Q(sqllog__icontains=search)
        | Q(user_display__icontains=search)
        | Q(alias__icontains=search)
    )

    sql_log_count = sql_log.count()
    sql_log_list = sql_log.order_by("-id")[offset:limit].values(
        "id",
        "instance_name",
        "db_name",
        "sqllog",
        "effect_row",
        "cost_time",
        "user_display",
        "favorite",
        "alias",
        "create_time",
    )
    # QuerySet 序列化
    rows = [row for row in sql_log_list]
    result = {"total": sql_log_count, "rows": rows}
    # 返回查询结果
    return HttpResponse(
        json.dumps(result, cls=ExtendJSONEncoder, bigint_as_string=True),
        content_type="application/json",
    )


@permission_required("sql.menu_sqlquery", raise_exception=True)
def favorite(request):
    """
    收藏查询记录，并且设置别名
    :param request:
    :return:
    """
    query_log_id = request.POST.get("query_log_id")
    star = True if request.POST.get("star") == "true" else False
    alias = request.POST.get("alias")
    QueryLog(id=query_log_id, favorite=star, alias=alias).save(
        update_fields=["favorite", "alias"]
    )
    # 返回查询结果
    return HttpResponse(
        json.dumps({"status": 0, "msg": "ok"}), content_type="application/json"
    )


def kill_query_conn(instance_id, thread_id):
    """终止查询会话，用于schedule调用"""
    instance = Instance.objects.get(pk=instance_id)
    query_engine = get_engine(instance)
    query_engine.kill_connection(thread_id)


@permission_required("sql.menu_sqlquery", raise_exception=True)
def generate_sql(request):
    """
    利用AI生成查询SQL, 传入数据基本结构和查询描述
    :param request:
    :return:
    """
    query_desc = request.POST.get("query_desc")
    db_type = request.POST.get("db_type")
    if not query_desc or not db_type:
        return HttpResponse(
            json.dumps({"status": 1, "msg": "query_desc or db_type不存在", "data": []}),
            content_type="application/json",
        )

    instance_name = request.POST.get("instance_name")
    try:
        instance = Instance.objects.get(instance_name=instance_name)
    except Instance.DoesNotExist:
        return HttpResponse(
            json.dumps({"status": 1, "msg": "实例不存在", "data": []}),
            content_type="application/json",
        )
    db_name = request.POST.get("db_name")
    schema_name = request.POST.get("schema_name")
    tb_name = request.POST.get("tb_name")

    result = {"status": 0, "msg": "ok", "data": ""}
    try:
        query_engine = get_engine(instance=instance)
        query_result = query_engine.describe_table(
            db_name, tb_name, schema_name=schema_name
        )
        openai_client = OpenaiClient()
        # 有些不存在表结构, 例如 redis
        if len(query_result.rows) != 0:
            result["data"] = openai_client.generate_sql_by_openai(
                db_type, query_result.rows[0][-1], query_desc
            )
        else:
            result["data"] = openai_client.generate_sql_by_openai(
                db_type, "", query_desc
            )
    except Exception as msg:
        result["status"] = 1
        result["msg"] = str(msg)
    return HttpResponse(json.dumps(result), content_type="application/json")


def check_openai(request):
    """
    校验openai配置是否存在
    :param request:
    :return:
    """
    config_validate = check_openai_config()
    if not config_validate:
        return HttpResponse(
            json.dumps(
                {
                    "status": 1,
                    "msg": "openai 缺少配置, 必需配置[openai_base_url, openai_api_key, default_chat_model]",
                    "data": False,
                }
            ),
            content_type="application/json",
        )

    return HttpResponse(
        json.dumps({"status": 0, "msg": "ok", "data": True}),
        content_type="application/json",
    )
