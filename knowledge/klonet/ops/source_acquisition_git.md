---
title: Klonet 源码获取与 Git SSH 配置
status: current_runbook
priority: P0
domains: operations, deployment, source_control
intent_tags: platform_start, source_acquisition, git_setup
last_verified: 2026-07-06
---

# Klonet 源码获取与 Git SSH 配置

## 适用场景

用于在新服务器、新账号或新平台实例上准备 Klonet 平台源码。基础环境安装包 `vemu_install_new_gen` 只用于依赖、镜像和辅助脚本；后端和前端源码应通过 Git 拉取，或从一台已验证服务器复制完整项目副本。

## 标准源码来源

历史操作文档记录的后端仓库别名为：

```bash
git clone gitee:uestc-minenet/vemu_uestc.git
```

历史操作文档记录的前端仓库为：

```bash
git clone git@github.com:lht94/vemu-web.git
```

这些地址是历史资料中的源码来源。实际部署前仍要用当前服务器的 Git 配置、网络连通性和维护者确认的分支为准。不要把环境安装包、Docker 镜像包或 workspace 副本当成正在部署的平台源码。

## 配置 SSH Key

优先为当前服务器账号配置专用 Git SSH key，并把公钥登记到对应 Git 平台或仓库 deploy key。只有在维护者明确允许时，才从已验证服务器复制既有 `.ssh` 目录或私钥。

如果需要复用既有密钥，历史做法是把可用的 `.ssh` 目录复制到目标用户家目录，例如：

```bash
scp -r <source_user>@<source_host>:/home/<source_user>/.ssh/ /home/<target_user>/
```

密钥必须放在运行 Git 命令的目标用户家目录下，例如 `/home/<target_user>/.ssh/`。如果使用自定义私钥文件名，需要在 `~/.ssh/config` 中配置别名：

```sshconfig
Host gitee
    HostName gitee.com
    User git
    IdentityFile /home/<target_user>/.ssh/<gitee_private_key>
    IdentitiesOnly yes
```

常见权限要求：

```bash
chmod 700 ~/.ssh
chmod 600 ~/.ssh/<gitee_private_key>
chmod 644 ~/.ssh/<gitee_private_key>.pub
chmod 600 ~/.ssh/config
```

配置后先测试别名是否可用：

```bash
ssh -T gitee
```

## 拉取后端与前端代码

后端源码：

```bash
cd <project_parent>
git clone gitee:uestc-minenet/vemu_uestc.git
```

前端源码：

```bash
cd <frontend_parent>
git clone git@github.com:lht94/vemu-web.git
```

如果目标服务器不能访问 Git 仓库，可以从一台已验证服务器复制项目目录。复制时保留完整目录结构，并在复制后确认 `mains/`、`vemu_uestc/`、前端目录和必要启动文件是否存在。

## Git 身份与分支检查

每个克隆出来的仓库建议设置本地提交身份，避免污染全局配置：

```bash
git config --local user.name "<name>"
git config --local user.email "<email>"
```

操作前先确认当前仓库、远端和分支：

```bash
git remote -v
git branch -vv
git status
git log -3 --oneline
```

如果需要创建平台专用分支，应先切到正确基线分支，再创建并发布新分支。不要在未确认 `git status`、`git branch -vv` 和远端跟踪关系时执行回退、强推或批量同步。

## 回退远端提交的安全边界

历史文档中记录过“本地回退 + 远端回退”的流程，但这是高风险操作。执行前至少要满足：

- 已确认当前分支就是目标分支。
- 已用 tag 备份要回退的提交。
- 已确认没有其他人依赖该远端提交。
- 使用 `--force-with-lease`，不要使用裸 `--force`。

示例模板：

```bash
git status
git log -3 --oneline
git tag backup_drop_<short_sha> <short_sha>
git reset --hard HEAD~1
git push --force-with-lease origin <branch>
git log -3 --oneline
git cherry-pick backup_drop_<short_sha>
```

Ops Agent 在没有明确用户授权和受控计划时，不应执行 `git reset --hard`、`git push --force-with-lease` 或修改远端历史。
