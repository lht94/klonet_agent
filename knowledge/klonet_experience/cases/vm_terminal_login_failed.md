---
title: 虚机终端登录失败
status: diagnostic_playbook
priority: P0
domains: vm, terminal, websocket, libvirt
last_verified: 2026-06-23
---

# 虚机终端登录失败

## 现象

KVM 终端无法打开、连接后立即关闭、无输出，或前端 WebSocket 反复重连。

## 环境

当前 KVM 终端链路为前端 WebSocket -> Web Terminal 服务 -> `web_terminal_impl.py` -> libvirt domain/console stream。SSH 终端是另一条链路，不应混为一谈。

## 排查路径

1. 确认目标节点类型、所在 Worker、domain 名称和运行状态。
2. 在宿主机验证 libvirt 可见该 domain。
3. 检查 console 是否被其他会话占用，以及 domain 是否提供可用 console。
4. 检查 Web Terminal 进程、监听端口和日志。
5. 检查 Nginx/WebSocket 转发、前端目标地址和浏览器关闭码。
6. 绕过代理分层测试，确定故障在前端、代理、Terminal 服务还是 libvirt。

## 根因候选与确认标准

- domain 不存在或未运行：libvirt 查询无法得到可运行实例。
- console 被占用或未配置：本地 console 连接也失败。
- libvirt stream 异常：WebSocket 建立，但服务端 stream 创建或读取失败。
- 代理配置错误：本地直连成功，经 Nginx 后失败。
- 地址映射错误：前端连接的 Terminal 地址与当前实例配置不一致。

## 解决方案

修复已确认层并释放异常会话；避免直接销毁 domain。验证标准是终端持续连接、双向输入输出正常、关闭后线程与 stream 被清理。

## 相关源码

- klonet_knowledge/02_vemu_uestc_code/vemu_uestc/webserver/web_back/web_terminal_impl.py
- klonet_knowledge/02_vemu_uestc_code/mains/web_terminal_main.py
- knowledge/klonet_index/symbols.jsonl

## 相关文档

- klonet_knowledge/08_vm_terminal_docs/虚机web终端/虚机web终端登录文档.docx
- knowledge/klonet/flows/kvm_and_vm_networking.md

## 可复用结论

先区分 SSH 与 KVM console，再按 WebSocket、服务进程、libvirt stream、domain 四层定位。
