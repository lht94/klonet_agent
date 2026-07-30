from ..Implement_layer.DockerSwarmManager.MasterSwarmManager import MasterSwarmManager
from ..Implement_layer.DockerSwarmManager.WorkerSwarmManager import WorkerSwarmManager

class SwarmMaster(object):
    
    @staticmethod
    def docker_swarm_init():
        print('S层docker swarm init')
        
        return MasterSwarmManager.master_init()

    @staticmethod
    def docker_swarm_master_leave():
        print('S层docker swarm leave')
        
        return MasterSwarmManager.master_leave()
        

class SwarmWorker(object):

    @staticmethod
    def docker_join(token):
        print('in docker join')

        return WorkerSwarmManager.worker_join(token)

    @staticmethod
    def docker_leave():
        print('in docker leave')

        return WorkerSwarmManager.worker_leave()