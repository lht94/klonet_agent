---
title: Klonet Git 开发流程
status: team_safe_baseline
priority: P1
domains: development, git, collaboration
last_verified: 2026-06-22
---

# Klonet Git 开发流程

## 适用场景

用于 Klonet 后端、前端和知识库文档的日常协作。本文不保留个人账号、邮箱、私钥或仓库地址。

## 核心结论

每个开发者使用独立身份和密钥；基于明确目标分支创建功能分支；提交前检查 diff 和测试；共享分支优先 revert，不随意改写远端历史。

## 初始配置

~~~bash
git config --local user.name "<name>"
git config --local user.email "<email>"
git status
git remote -v
~~~

SSH：

- 每人生成独立密钥。
- .ssh 目录权限为 700。
- 私钥权限为 600。
- 公钥权限通常为 644。
- 通过 SSH config 设置 Host 和 IdentityFile。
- 不复制其他成员私钥。

## 开始开发

~~~bash
git fetch --all --prune
git switch <base_branch>
git pull --ff-only
git switch -c <type>/<short-topic>
~~~

开始前确认：

- 正确仓库。
- 正确基础分支。
- 工作区无误改。
- 任务范围清楚。
- 当前代码版本与部署版本对应。

## 提交

推荐格式：

~~~text
feat(scope): add capability
fix(scope): correct behavior
docs(scope): update knowledge
test(scope): add coverage
refactor(scope): simplify implementation
~~~

提交前：

~~~bash
git status --short
git diff
git diff --cached
<project test command>
~~~

不要提交密码、token、私钥、真实服务器地址、日志和大型生成物。

## 推送与同步

~~~bash
git push -u origin <branch>
~~~

落后时优先：

~~~bash
git fetch origin
git rebase origin/<base_branch>
~~~

发生冲突后逐文件理解，不使用 reset --hard 丢弃未知修改。

## 回退

### 未推送提交

根据情况使用 git restore、git reset --soft 或新提交修正。执行前先确认工作区和提交范围。

### 已推送共享提交

优先：

~~~bash
git revert <commit>
git push origin <branch>
~~~

### 必须改写历史

仅在团队确认后：

1. 创建备份分支或标签。
2. 确认当前分支和目标提交。
3. 使用 --force-with-lease。
4. 通知协作者重新同步。
5. 验证远端日志。

禁止在不确认分支时执行 reset --hard 和强制推送。

## 前后端多仓库

前端和后端可能来自不同仓库或版本包。每次协作记录：

- 前端仓库和提交。
- 后端仓库和提交。
- 接口版本或变更说明。
- 部署环境对应版本。

## 知识库变更

知识文档应与证据变更一起审查：

- 更新 last_verified。
- 保留证据路径。
- 标记规划态和历史态。
- 不把生成的 raw/OCR 文本当成人工结论。
- 运行敏感信息扫描。

## 常见问题

### 修改出现在错误仓库

先停下，保存 diff，确认 cwd、git rev-parse --show-toplevel 和 remote，再迁移改动。

### 前端更新后接口失败

核对前后端提交、config、API method、payload 和 Nginx，不假定仓库主分支天然兼容。

## 证据来源

- klonet_knowledge/02_vemu_uestc_code/doc/开发流程与规范/git开发流程和Python规范.md
- klonet_knowledge/06_quick_start_docs/git.md
- knowledge/staging/platform_operation_notes_lihetian_curated.md
