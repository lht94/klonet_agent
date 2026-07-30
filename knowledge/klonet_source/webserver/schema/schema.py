from jsonschema import validate, draft7_format_checker
from jsonschema.exceptions import SchemaError, ValidationError

def parameter_check(data,schema_data):
    try:
        validate(instance=data, schema=schema_data, format_checker=draft7_format_checker)
    except SchemaError as e:
        #print("验证模式schema出错:\n出错位置：{}\n提示信息：{}".format(e.path, e.message))
        raise ValueError("验证模式schema出错:\n出错位置：{}\n提示信息：{}".format(e.path, e.message))
    except ValidationError as e:
        print("json数据不符合schema规定:\n出错字段：{}\n提示信息：{}".format(e.path, e.message))
        return  {'code': 0, 'msg': "您填写的数据不符合规则，请修改! 错误信息：{}".format(e.message)}
    else:
        return  {'code': 1, 'msg': "schema参数检查通过！"}
