# Codex SSH Cluster Ops

[![Tests](https://github.com/xin1u/codex-ssh-cluster-ops/actions/workflows/tests.yml/badge.svg)](https://github.com/xin1u/codex-ssh-cluster-ops/actions/workflows/tests.yml)
[![License](https://img.shields.io/badge/License-Apache--2.0-blue.svg)](LICENSE)

A local-first Codex Skill for bounded, auditable SSH operations across shared compute clusters. It uses native OpenSSH and the Python standard library, with no remote Codex installation or third-party SSH MCP required.

这是一个可共享的 Codex Skill，让同事用本地 Codex 管理已有 SSH 集群，而不需要在每台服务器安装或登录 Codex。

它提供四类常用能力：

- 并行巡检主机、GPU、进程名、tmux 和 Git 状态；
- 复用 OpenSSH `ControlMaster` 持久连接，减少跳板机和重复握手开销；
- 将本地完整 Git worktree 以审核过的 diff 同步到指定远端仓库；
- 用本地命令文件启动、查看日志并精确停止一个受管 tmux 会话。

实现只依赖原生 OpenSSH、Git 和 Python 标准库。它不依赖 Node、npm、第三方 SSH MCP、远端 Codex、远端 API key 或自定义 `apply_patch` wrapper。

## 安装

支持 macOS 或 Linux 本地环境，要求 Python 3.9+、OpenSSH 和 Git。服务器需要 Bash、Git、GNU `realpath`、`flock`、`sha256sum`；使用会话管理时还需要 tmux。

从 GitHub 安装到 Codex Skills 目录：

```bash
git clone https://github.com/xin1u/codex-ssh-cluster-ops.git \
  ~/.codex/skills/codex-ssh-cluster-ops
```

也可以下载 release 或源码压缩包并解压：

```bash
mkdir -p ~/.codex/skills
tar -xzf codex-ssh-cluster-ops.tar.gz -C ~/.codex/skills
```

开启一个新的 Codex 会话后，可以用 `$codex-ssh-cluster-ops` 明确触发。

## 首次配置

先确保 `~/.ssh/config` 里已有可用 alias，且目标主机公钥已经通过团队现有流程写入 `known_hosts`。本 Skill 不自动接受未知公钥。

复制策略模板：

```bash
mkdir -p ~/.config/codex-ssh-cluster-ops
cp ~/.codex/skills/codex-ssh-cluster-ops/assets/policy.example.json \
  ~/.config/codex-ssh-cluster-ops/policy.json
chmod 600 ~/.config/codex-ssh-cluster-ops/policy.json
```

为每台机器填写：

- SSH alias；
- `ssh -G <alias>` 实际解析出的 user、hostname、port；
- 远端 `hostname` 的精确输出；
- 允许操作的代码仓库根目录；
- 允许创建新运行目录的根目录；
- 是否允许补丁和受管 tmux 会话。

模板默认关闭补丁、受管会话和持久连接。先完成只读验证，再只为确实需要的同事显式打开相应能力。策略中不要写私钥、密码、token 或其他凭据。验证配置：

```bash
python3 ~/.codex/skills/codex-ssh-cluster-ops/scripts/clusterctl.py \
  validate-policy

python3 ~/.codex/skills/codex-ssh-cluster-ops/scripts/clusterctl.py \
  doctor --all
```

完整字段说明见 [references/configuration.md](references/configuration.md)。

## 日常使用

最方便的方式是直接告诉本地 Codex 目标和边界。例如只读巡检：

```text
请使用 $codex-ssh-cluster-ops 并行巡检策略里的所有机器，报告 GPU、进程、tmux 和指定仓库 Git 状态；只读，不启动或停止任何任务。
```

同步本地修改并启动 debug：

```text
请使用 $codex-ssh-cluster-ops：先核对本地和 gpu-a 的 HEAD；把本地完整修改生成 diff 给我审核；审核后同步到远端，并从精确代码 tree 用 debug-r1 这个 tmux 名启动 command.sh。不要影响其他会话。
```

查看或停止一个精确任务：

```text
请使用 $codex-ssh-cluster-ops 查看 gpu-a 上 debug-r1 的状态和最后 200 行日志。
```

```text
请使用 $codex-ssh-cluster-ops 优雅停止 gpu-a 上精确名称为 debug-r1 的受管会话，不删除输出，不影响同名前缀的其他会话。
```

底层完整工作流为：

```text
validate-policy -> doctor/audit
                -> make-diff -> 人工审核 -> apply-diff
                -> session-start -> session-status/session-log -> session-stop
```

具体命令见 [references/operations.md](references/operations.md)。

## 重要边界

- 策略 allowlist 只是工具可以触达的技术范围，不等于某个操作已获授权。
- `session-start` 的命令文件会以远端账号权限执行，它是可审计的异步执行入口，不是安全沙箱；启动前必须审核内容。
- 不提供通用交互 shell、临时 `exec`、任意上传下载、批量 kill、自动 Git reset/clean、提交、push 或删除输出。
- 所有主机使用严格 host-key 检查，并同时核对 `ssh -G` 解析身份和远端 user/hostname。
- diff 和命令内容通过 SSH stdin 发送，不进入 SSH argv；本地审计日志只记录 hash 和元数据。
- 远端补丁要求精确 HEAD、clean tree、仓库锁和结果 tree 校验；启动会话要求本地与远端 Git 可见 tree 相同。Git ignored 文件和外部环境不在该指纹内，含 submodule 的仓库不支持受管会话。
- 停止操作只接受精确 tmux 名，并要求 `--confirm-name` 再确认一次。

完整安全模型见 [references/security-model.md](references/security-model.md)。

## 验证

```bash
cd ~/.codex/skills/codex-ssh-cluster-ops
python3 -m unittest discover -s tests -v
python3 -m compileall -q scripts tests
```

测试包含策略 fail-closed、严格 SSH 参数、完整 Git diff/tree、payload 不进入 argv/审计、tmux 精确匹配，以及 fake SSH 的端到端转发流程；测试不会连接真实集群。

## License

本项目采用 [Apache License 2.0](LICENSE)。
