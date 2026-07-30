# 初始角色
initial_roles = [
    "ordinary_user", # 普通用户 
    "admin", # 管理员,
    "super_admin", # 超级管理员
]

# 初始权限表
initial_authorities = [
    "UserLoginAPI.get",
    "UserLoginAPI.delete",
    "UserLoginAPI.put",
    "SuperDelete.post",
    "ImageManage.get"
]

# 各角色初始权限
initial_role_authority = {
    "ordinary_user":[
        "UserLoginAPI.get",
    ],
    "admin":[
        "UserLoginAPI.get",
        "UserLoginAPI.delete",
        "ImageManage.get"
    ],
    "super_admin":[
        "UserLoginAPI.get",
        "UserLoginAPI.delete",
        "UserLoginAPI.put",
        "SuperDelete.post",
        "ImageManage.get"
    ],
}