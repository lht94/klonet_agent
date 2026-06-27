---
title: Klonet 拓扑节点类型
status: current_verified
priority: P0
domains: topology, vm
intent_tags:
  - platform_usage
  - topology_deploy
last_verified: 2026-06-28
---

# Klonet 拓扑节点类型

## 适用场景

用于回答“拓扑里能放置哪些节点”“Klonet 支持哪些节点类型”“实验拓扑节点面板有什么类型”等普通使用和源码理解问题。

## 核心结论

当前可确认的拓扑节点类型包括：

| 节点类型 | 含义 | 主要证据 |
| --- | --- | --- |
| Host | 主机类节点，通常用于业务端点、流量端点或普通容器节点 | `knowledge/klonet/03_domain_terms.md`、`vemu_uestc/Function_layer/topo_preprocess.py` 中 `Ne_host` |
| Switch | 交换类节点，可由 OVS 等能力实现二层连接 | `knowledge/klonet/03_domain_terms.md`、`Ne_switch`、`OVSStartError` |
| Router | 路由类节点，用于三层转发或路由实验 | `knowledge/klonet/03_domain_terms.md`、`Ne_router` |
| Controller | SDN 控制器类节点，例如 ONOS/Ryu 相关控制场景 | `knowledge/klonet/03_domain_terms.md`、`Ne_controller` |
| KVM 虚机节点 | 平台管理的虚机节点，不等同于宿主机上手工创建的维护虚机 | `knowledge/klonet/flows/kvm_and_vm_networking.md`、`Service_layer/NEManager.py` |

这些节点不是宿主机本身。宿主机是运行 Worker 和真实资源的服务器；拓扑节点是平台根据拓扑数据创建出来的容器、虚机或网络设备。

## 源码索引证据

机器索引中可以看到以下与节点类型相关的符号：

- `vemu_uestc/Function_layer/topo_preprocess.py`
  - `Ne_host`
  - `Ne_switch`
  - `Ne_router`
  - `Ne_controller`
  - `_hosts_handle`
  - `_switches_handle`
  - `_routers_handle`
  - `_controllers_handle`
- `vemu_uestc/Function_layer/topo_aggregate.py`
  - `Ne_re_host`
  - `Ne_re_switch`
  - `Ne_re_router`
  - `Ne_re_controller`
- `vemu_uestc/Service_layer/TopoManager.py`
  - `_create_ne`：根据节点类型分发创建节点。
- `vemu_uestc/Service_layer/NEManager.py`
  - KVM、容器和节点服务相关能力。

## 回答边界

如果用户只问“能放哪些节点”，可以直接回答 Host、Switch、Router、Controller、KVM 虚机节点，并说明不同部署版本的前端节点面板可能会隐藏或扩展部分选项。

如果用户追问精确枚举、字段名或前端面板展示名称，应继续检索 `knowledge/klonet_index` 或读取当前部署版本源码。当前知识库保存的是源码索引和 curated 文档，不保证每个部署现场的前端选项完全一致。

## 相关知识

- [Klonet 领域术语](../03_domain_terms.md)
- [Klonet 拓扑部署流程](topology_deploy.md)
- [Klonet KVM 与虚机组网](kvm_and_vm_networking.md)
