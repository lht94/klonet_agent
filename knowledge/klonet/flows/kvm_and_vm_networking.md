---
title: Klonet KVM 与虚机组网
status: current_verified_with_environment_validation
priority: P0
domains: kvm, vm, networking, terminal, images
last_verified: 2026-06-22
---

# Klonet KVM 与虚机组网

## 适用场景

用于理解平台如何从 Docker 节点扩展到 KVM 虚机节点，以及镜像、创建、链路、终端、SSH、端口映射和排障边界。

## 核心结论

平台 KVM 节点不是宿主机上任意 virsh 虚机。平台会根据拓扑和镜像元数据生成 qcow2 实例，通过 virt-install/libvirt 创建 domain，使用 tap、bridge、Veth 或 OVS 连接链路，并把状态写入平台数据库。

## 前置条件

- 宿主机支持 KVM。
- libvirtd 正常。
- virsh、virt-install、qemu-img 可用。
- 镜像目录、权限和磁盘空间满足要求。
- Worker 有执行虚拟化和网络命令的权限。
- libvirt 网络或平台网桥已准备。
- Web Terminal/SSH 相关服务和端口映射可用。
- Master 与 Worker 的 KVM 镜像元数据一致。

## KVM 节点定义

vemu_api 示例显示，节点需要：

- image：KVM 镜像对象。
- service：设置为 kvm。
- resource_limit：vCPU 和内存。
- vm_port_num：虚机端口数量。
- 节点类型和可选配置。

涉及虚机的链路需要显式指定虚机侧端口索引。端口从 1 开始，不能超过 vm_port_num。

不得复制示例中的用户、后端地址或镜像名称。

## 镜像生命周期

### 查询

前端或 API 获取用户可用的 KVM 镜像和默认镜像。

### 上传

Master 接收上传，记录 MySQL 元数据，并向相关 Worker 分发或同步镜像。

### 实例化

NEManager 根据默认或用户镜像路径生成拓扑实例 qcow2，并组合 virt-install 参数创建 domain。

### 删除

删除用户镜像前确认：

- 没有运行中的拓扑使用。
- Worker 上的副本状态。
- MySQL 元数据和文件一致。
- 不删除默认共享镜像。

KVM 镜像操作耗时且占用大量磁盘，应监控进度和失败恢复。

## 节点创建流程

~~~text
拓扑 JSON 中 service=kvm
-> 拓扑预处理分配端口/网桥信息
-> Worker NEManager 准备 qcow2
-> virt-install 创建 libvirt domain
-> 创建 tap/bridge/OVS 连接
-> 写入 NEid、端口和网络状态
-> 启动终端或节点服务
~~~

创建失败时可能留下 qcow2、domain、bridge、tap 或数据库记录，恢复前逐项核对。

## 组网模型

平台代码包含：

- KVM-KVM 链路。
- KVM-Docker 混合链路。
- KVM 与 bridge/OVS 连接。
- 跨 Worker 网络。

create_kvm_link 使用两侧虚机 ID、Veth 和 bridge 信息建立 KVM 链路。NEManager 还会创建 bridge、tap 并将 tap 加入网桥。

拓扑描述中的 src_port/dst_port 对虚机侧非常重要；IP 参数未必直接配置在虚机侧接口上。

## 网络层次

~~~text
虚机网卡
-> tap
-> 平台 bridge/OVS
-> Veth 或 VXLAN
-> 对端 bridge/OVS
-> 对端 tap/容器网卡
~~~

实际结构因节点类型和同机/跨机而变化。排障时从虚机网卡向外逐层检查。

## NAT 与管理网络

libvirt 默认网络通常用于虚机管理和上网，平台实验链路可能使用额外网卡。

多网卡虚机应明确：

- 哪张网卡是管理/上网。
- 哪张网卡是实验互联。
- 默认路由在哪张网卡。
- DNS 和 MTU。
- 平台端口索引与虚机网卡名称的对应。

历史文档中的固定网段可能有笔误，不应直接复制。

## Web Terminal

webserver/web_back/web_terminal_impl.py 使用 libvirt：

- 打开 libvirt 连接。
- 获取 domain 状态。
- 对运行或暂停 domain 打开 console。
- 使用 stream 读取输出。
- 通过 WebSocket 发送到前端。
- KVMBeatWS 维护终端相关线程。

终端异常时检查 domain、console 占用、libvirt stream、WebSocket 和 Terminal 服务。

## SSH 与端口映射

平台提供 Master/Worker SSH 服务 API 和端口映射 API。Worker 根据节点类型、所在 Worker 和数据库状态完成实际操作。

安全要求：

- 不保存默认 root 密码。
- 不把 SSH 开放到不受控网络。
- 宿主机映射端口必须唯一。
- 映射状态写入数据库并与真实监听一致。
- 关闭服务时回收端口。

历史实现可能在节点内安装 SSH 并修改 root 登录配置，正式环境应经过安全评审。

## 虚机生命周期操作

安全操作：

~~~bash
virsh list --all
virsh start <domain>
virsh shutdown <domain>
virsh console <domain>
~~~

危险操作：

- virsh destroy。
- virsh undefine。
- 直接删除 qcow2。
- 在有快照时扩容。
- 强制 console 抢占连接。

必须确认 domain 属于目标用户和拓扑。

## 常见问题

### virt-install 失败

检查镜像路径、权限、磁盘、domain 重名、网络/bridge、CPU 虚拟化和命令参数。

### 虚机存在但平台显示失败

对比 libvirt domain、Redis 节点表、NEid、qcow2 路径和 Worker 任务返回。可能是底层成功但状态写入失败。

### 虚机接口未出现或链路不通

检查 vm_port_num、端口索引、tap、bridge/OVS、网卡 up 状态、IP、MTU 和跨机 VXLAN。

### Web Terminal 立即关闭

检查 domain 运行状态、旧 console、libvirt stream、Terminal 服务和浏览器 WebSocket。

### SSH 无法连接

检查节点 SSH 服务、宿主机映射、Worker 地址、防火墙、端口占用和数据库映射记录。

### 删除后仍有 domain 或磁盘

检查 Worker 可达性和删除任务，随后定向处理 domain、network、qcow2 与数据库，禁止批量清理。

## 验证清单

- KVM 镜像可查询。
- 创建一个最小虚机节点。
- libvirt domain 与数据库 NEid 一致。
- Terminal 能连接和退出。
- SSH 映射可创建和回收。
- 创建一条虚机链路并验证端口。
- 删除拓扑后无 domain、tap、bridge、qcow2 临时文件和 Redis 遗留。
- 混合 Docker/KVM 场景单独验证 MTU 和校验和设置。

## 关键文件

- Service_layer/NEManager.py
- Service_layer/kvm_image_upload.py
- Service_layer/kvm_image_sync.py
- Implement_layer/LinkManager/link_operate.py
- webserver/web_back/web_terminal_impl.py
- webserver/api/kvm_image/
- webserver/api/dynamic_modify/worker_kvm_api.py
- webserver/api/ssh_connect/
- tools/vm_topo_api_demo.py
- libvirt_config.sh

## 证据来源

- klonet_knowledge/02_vemu_uestc_code/Service_layer/NEManager.py
- klonet_knowledge/02_vemu_uestc_code/Implement_layer/LinkManager/link_operate.py
- klonet_knowledge/02_vemu_uestc_code/webserver/web_back/web_terminal_impl.py
- klonet_knowledge/02_vemu_uestc_code/tools/vm_topo_api_demo.py
- klonet_knowledge/07_vm_related/
- klonet_knowledge/08_vm_terminal_docs/
- knowledge/staging/platform_operation_notes_lihetian_curated.md
