---
title: KVM 节点组网异常
status: diagnostic_playbook
priority: P0
domains: kvm, ovs, bridge, networking
last_verified: 2026-06-23
---

# KVM 节点组网异常

## 现象

KVM 节点已创建但接口缺失、KVM-KVM 或 KVM-Docker 链路不通、只能单向通信，或多网卡节点默认路由异常。

## 环境

平台通过 qcow2/libvirt 创建 domain，并以 tap、bridge、veth 或 OVS 连接链路。宿主机设备存在不代表虚机内部接口已配置。

## 排查路径

1. 从拓扑数据确认两端节点、端口与所在 Worker。
2. 在 libvirt domain XML 中确认虚机接口与 tap。
3. 在宿主机确认 tap、veth、bridge/OVS 端口及其归属。
4. 对照链路两端的 UP 状态、MTU、MAC 和地址。
5. 进入虚机检查接口命名、IP、路由和多网卡默认路由。
6. 跨主机时检查 Overlay/VXLAN、宿主机防火墙和校验和/MTU。
7. 将平台数据库记录与真实设备逐项对照。

## 根因候选与确认标准

- tap 未加入正确桥：domain 有接口，但桥或 OVS 端口列表缺失。
- 端口映射错误：拓扑中的 src/dst port 与真实接口不一致。
- 虚机内未配置：宿主链路正常，虚机接口无地址或未启用。
- 默认路由冲突：多网卡存在多个默认路由或业务流量走错接口。
- MTU/Overlay 问题：小包可通、大包失败，或仅跨主机失败。

## 解决方案

从最靠近故障边界的一层修复，避免同时重建 domain 和宿主网络。修复后按同宿主、跨宿主、双向通信和拓扑删除四个动作验证。

## 相关源码

- klonet_knowledge/02_vemu_uestc_code/vemu_uestc/Service_layer/NEManager.py
- klonet_knowledge/02_vemu_uestc_code/vemu_uestc/Implement_layer/LinkManager/
- knowledge/klonet_index/domain_map.jsonl

## 相关文档

- knowledge/klonet/flows/kvm_and_vm_networking.md
- klonet_knowledge/07_vm_related/虚拟机组网全过程指南.docx

## 可复用结论

按“domain 接口 -> tap -> bridge/OVS -> 宿主链路 -> 虚机网络栈”排查，可避免在多层同时改动。
