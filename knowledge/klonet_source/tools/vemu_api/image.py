from .common import Manager, Image

class ImageManager(Manager):
    '''镜像管理类

    Attributes:
        user(str): 用户名
    '''
    def __init__(self, user_name, backend_ip=None, backend_port=None):
        super().__init__(backend_ip, backend_port)
        self.user = user_name

    def get_images(self, quiet=False):
        '''获取当前用户的所有镜像名及镜像对象。

        Args:
            quiet(bool): 默认为False。若为False，则将打印镜像名列表；否则将关闭打印。

        Returns:
            一个字典，key为镜像名，value为Image对象
        '''
        resp = self._get("/my/image/", params={"username": self.user})
        resp_json = self._parse_resp(resp)
        try:
            self._check_resp_code(resp_json)
        except KeyError:
            pass
        image_list = {}
        for registry_type in resp_json.keys(): # 镜像仓库类型，如private/public
            for type in resp_json[registry_type].keys():# 镜像类型，如host/switch
                for image_dict in resp_json[registry_type][type]:
                    # 注意这里的subtype为前端显示的镜像名，在这里同样用作镜像名来向
                    # 用户屏蔽细节
                    image_list[image_dict["subtype"]] = Image(**image_dict)

        if not quiet:
            print(f"{self.user} has these images: {list(image_list.keys())}")
            
        return image_list
    
    def get_kvm_image(self, quiet=False):
        '''
        获取当前用户可使用的kvm镜像
        
        Args:
            quiet(bool): 默认为False。若为False，则将打印镜像名列表；否则将关闭打印。
            
        Returns:
            一个字典，key为镜像名，value为Image对象
        '''
        resp = self._get("/my/kvm_image/", params={"user": self.user})
        resp_json = self._parse_resp(resp)
        try:
            self._check_resp_code(resp_json)
        except KeyError:
            pass
        image_list = {}
        for registry_type in resp_json.keys():
            for type in resp_json[registry_type].keys():
                for image_dict in resp_json[registry_type][type]:
                    image_list[image_dict["subtype"]] = Image(**image_dict)  # 仅仅是实例化，可以与容器用同一套
        
        if not quiet:
            print(f"{self.user} has these kvm_images: {list(image_list.keys())}")

        return image_list
    
    def get_hardware(self, type):
        '''
        获取当前用户可使用的所有硬件设备  
            
        Returns:
            一个字典，列举所有的此用户可用设备
        '''
        resp = self._get("/my/hardware/", params={"user": self.user, "type":type})
        resp_json = self._parse_resp(resp)
        try:
            self._check_resp_code(resp_json)
        except KeyError:
            pass
        resp_list = []
        for value in resp_json.values():
            resp_list.append(value['id'])
        return resp_list
    
    def get_hardware_config(self, id, type):
        '''
        获取硬件设备以及配置信息 
            
        Returns:
            一个字典，列举设备的所有配置信息
        '''
        resp = self._get("/my/id_hardware/", params={"id": id, "type":type})
        resp_json = self._parse_resp(resp)
        try:
            self._check_resp_code(resp_json)
        except KeyError:
            pass

        return resp_json