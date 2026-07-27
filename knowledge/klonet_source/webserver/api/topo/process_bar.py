from flask.views import MethodView
from flask import request
import json
import grequests
from celery.result import AsyncResult
from flask_login import login_required
from ....tools.context import redis_context
from ....webserver import celery
from ....vemu_config.config import PROJ_CONFIG
from ....Implement_layer.LinkManager import shell_execute
from ....tools.schema.schema import parameter_check
from ....tools.schema.process_bar_schema import *


class ProcessBarAPI(MethodView):
     """
     进度条 API，前端访问后端包含用户名和拓扑名的 json
     后端查询 celery 任务队列返回拓扑创建情况：
          - 情况一, 创建任务报错失败, 返回 code 0, msg 字段 "拓扑发生错误：" + celery报错msg  + \n拓扑创建失败，即将进行拓扑删除！
          - 情况二, 创建任务还未结束, 返回 code 1, msg 字段 "获取进度成功！"
          - 情况三, 创建任务成功结束, 返回 code 1, msg 字段 "成功！" 
     除却情况二（过程未完成，未到100%），其他两种情况需要删除对应 pb_table
     """

     def post(self):

          try:
               # 从请求中的 json 获得用户数据
               data = json.loads(request.get_data(as_text=True))
               # 参数检查
               result = parameter_check(data, schema_process_bar)
               if result['code'] == 0:
                    return {'code': 0, 'msg': result['msg']}
               # 信息提取
               user, topo, usage = data['user'], data['topo'], data['usage']
               # 存放在redis中的进度条表项名称
               pb_table_name = f"{PROJ_CONFIG.pb_table_name_prefix}_{topo}_{usage}"

               with redis_context(user) as user_db_cli:

                    # 获得当前进度值
                    vals = user_db_cli.get_all_values(pb_table_name).values()
                    # 累加进度条表项中的各值，并返回前端
                    sum = 0
                    for val in vals:
                         if isinstance(val, float):
                              sum += float(val)
                    process_value = round(sum)

                    # 查询celery任务的任务id
                    task_id = user_db_cli.get_value(pb_table_name, 'task_id')
                    # 查询celery任务的当前运行情况
                    deploy_task_result = AsyncResult(id=task_id, app=celery)

                    # 若task结束，则返回结果
                    if deploy_task_result.ready():
                         # 获得 task 返回内容
                         task_ret = deploy_task_result.get()
                         # 情况一
                         if task_ret['code'] == 0:
                              if usage == 'deploy':
                                   return {
                                        "code": 0,
                                        "process_value": process_value,
                                        "msg": f"创建拓扑发生错误：{task_ret['msg']}"
                                                "\n拓扑创建失败，即将进行拓扑删除！"
                                   }
                              elif usage == 'delete':
                                   return {
                                        "code": 0,
                                        "process_value": process_value,
                                        "msg": f"删除拓扑发生错误：{task_ret['msg']}"
                                                "\n拓扑删除失败，请联系管理员进行删除！"
                                   }
                         # 情况三
                         else:
                              if usage == 'deploy':
                                   return {
                                        "code": 1,
                                        "process_value": 100,
                                        "msg": "拓扑创建成功！"
                                   }
                              elif usage == 'delete':
                                   return {
                                        "code": 1,
                                        "process_value": 100,
                                        "msg": "拓扑删除成功！"
                                   }
                    # 情况二
                    else:
                         # 后端收到进度请求，查询任务当前运行情况
                         # 当task未完成且进度值为100时，会进到这里
                         # 前端收到进度值为100，认为task已完成，不再请求进度
                         # 导致 “数据库进度表未删” 和 “显示"获取进度成功！"” 同时发生
                         # 通过下面两行，使进度值为99，前端会认为task未结束
                         # 一段时间后，前端再次请求进度……待task完成后进度值为100
                         if process_value == 100:
                              process_value = 99
                         return {
                              "code": 1,
                              "process_value": process_value,
                              "msg": "获取进度成功！"
                         }
          
          except Exception as e:
               # 创建拓扑时，进度条功能发送错误的返回值
               if usage == 'deploy':
                    return {
                         "code": 0,
                         "process_value": None,
                         "msg": f"进度条功能请求发生错误：{str(e)}"
                                 "\n但拓扑创建仍在进行中，请稍后手动刷新页面，\n"
                                 "以确定拓扑是否创建成功！"
                    }
               # 删除拓扑时，进度条功能发送错误的返回值
               elif usage == 'delete':
                    return {
                         "code": 0,
                         "process_value": None,
                         "msg": f"进度条功能请求发生错误：{str(e)}" + \
                                 "\n但拓扑删除仍在进行中，请稍后手动刷新页面，\n"
                                 "以确定拓扑是否删除成功！"
                    }
               
     def delete(self):
          """
          处理删除服务的HTTP请求
          """
          return {'msg': 'this url can be routed', 'code': 1}

     def get(self):
          """
          处理GET服务的HTTP请求
          """
          return {'msg': 'this url can be routed', 'code': 1}


class DownloadProcessMasterAPI(MethodView):
     """
     文件下载进度条
     /master/download_process/
     """
     def post(self):
          # 信息提取
          data = json.loads(request.get_data(as_text=True))
          user, topo, ne, file_path = \
               data['user'], data['topo'], data['ne'], data['file']
          # 读取数据库，获得节点容器id和所在worker的ip
          table_name = f'{topo}_{ne}'
          with redis_context(user) as user_db_cli:
               ne_id = user_db_cli.get_value(table_name, 'NEid')
               subtopo = user_db_cli.get_value(table_name, 'NEloc')
               worker_ip = user_db_cli.get_value('subtopo2worker', subtopo)
          # 请求worker的url
          req_url = f'http://{worker_ip}:{PROJ_CONFIG.worker_port}/worker/download_process/'
          dict_info = {
               'user': user,
               'topo': topo,
               'ne': ne,
               'ne_id': ne_id,
               'file_path': file_path
          }
          rs = (grequests.post(req_url, json=dict_info),)
          resp = grequests.map(rs)[0]
          return resp.json()


class DownloadProcessWorkerAPI(MethodView):
     """
     文件下载进度条
     /worker/download_process/
     """
     def post(self):
          try:
               # 信息提取
               data = json.loads(request.get_data(as_text=True))
               user, topo, ne, ne_id, file_path = data['user'], data['topo'], \
                    data['ne'], data['ne_id'], data['file_path']
               # 容器里文件的大小
               container_file_byte = shell_execute(f"docker exec {ne_id} "
                                                  f"sh -c 'stat -c %s {file_path}'")
               # 本地文件的大小
               folder_name = f'/root/vemu_static/{user}_{topo}_{ne}'
               file_name = file_path.split('/')[-1]
               local_file_byte = shell_execute(f"stat -c %s {folder_name}/{file_name}")
               # 求解进度
               return {"code": 1,
                       "process_value": round(int(local_file_byte) / int(container_file_byte) * 100),
                       "msg": "获取进度成功！"}
          
          except Exception as e:
               return {"code": 0,
                       "process_value": None,
                       "msg": f"进度错误：{str(e)}"}


class UploadProcessMasterAPI(MethodView):
     """
     文件上传进度条
     /master/upload_process/

     (无用，因为上传文件的瓶颈在于拿到request)
     (文件存储和宿主机到容器的文件传输都相对快速)
     """
     def post(self):
          # 信息提取
          data = json.loads(request.get_data(as_text=True))
          user, topo, ne, file_path = \
               data['user'], data['topo'], data['ne'], data['file']
          # 读取数据库，获得节点容器id和所在worker的ip
          table_name = f'{topo}_{ne}'
          with redis_context(user) as user_db_cli:
               ne_id = user_db_cli.get_value(table_name, 'NEid')
               subtopo = user_db_cli.get_value(table_name, 'NEloc')
               worker_ip = user_db_cli.get_value('subtopo2worker', subtopo)
          # 请求worker的url
          req_url = f'http://{worker_ip}:{PROJ_CONFIG.worker_port}/worker/upload_process/'
          dict_info = {
               'user': user,
               'topo': topo,
               'ne': ne,
               'ne_id': ne_id,
               'file_path': file_path
          }
          rs = (grequests.post(req_url, json=dict_info),)
          resp = grequests.map(rs)[0]
          return resp.json()


class UploadProcessWorkerAPI(MethodView):
     """
     文件上传进度条
     /worker/upload_process/

     (无用，因为上传文件的瓶颈在于拿到request)
     (文件存储和宿主机到容器的文件传输都相对快速)
     """
     def post(self):
          try:
               # 信息提取
               data = json.loads(request.get_data(as_text=True))
               user, topo, ne, ne_id, file_path = data['user'], data['topo'], \
                    data['ne'], data['ne_id'], data['file_path']
               # 容器里文件的大小
               try:
                    container_file_byte = shell_execute(f"docker exec {ne_id} "
                                                       f"sh -c 'stat -c %s {file_path}'")
               except:
                    container_file_byte = 0
               # 本地文件的大小
               folder_name = f'/root/vemu_static/upload/{user}_{topo}_{ne}'
               file_name = file_path.split('/')[-1]
               local_file_byte = shell_execute(f"stat -c %s {folder_name}/{file_name}")
               # 求解进度
               return {"code": 1,
                    "process_value": round(int(container_file_byte) / int(local_file_byte) * 100),
                    "msg": "获取进度成功！"}
          
          except Exception as e:
               return {"code": 0,
                       "process_value": None,
                       "msg": f"进度错误：{str(e)}"}
