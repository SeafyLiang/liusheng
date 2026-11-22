# 留声开放准备方案

## 当前状态

此仓库为 SeafyLiang 名下的 private 私有展示仓库，仅包含介绍、路线图与贡献文档。没有业务源码、部署包或 GitHub Release，尚未完成开源授权。只有维护者授权的账号能够查看。

本轮不变更 Gitea、原工作区、NAS 服务或仓库公开性。转为 public 必须等待用户明确确认。

## 为什么保留两处仓库

Gitea 保留日常开发与内部运维上下文；GitHub 承担公开展示与社区协作。公开部分应清晰描述可部署能力和可贡献模块，内部主机地址、账号配置与运行证据不直接暴露给外部用户。

## 同步策略

当前私有预览采用独立文档历史，不是 Gitea 的镜像。后续不能直接用全量镜像覆盖这个仓库。

源码迁入前需要确定一种一致的维护方式：在 Gitea 管理经过审核的公开分支，仅向 GitHub 同步选定分支和发布标签；预览文档应先纳入该公开分支，核对提交历史后再接续。现有预览历史与主仓库不同，整合方案需单独评审，不自动强推或改写历史。

GitHub PR 由维护者审核后导入 Gitea，再同步回 GitHub，并在原 PR 标记处理结果。Issue、PR、讨论、发布附件分别维护，不把代码镜像当作社区数据同步。

## 公开前的必要准备

- 核对拟发布文件与历史中的凭据、私人域名、内网地址、数据库、音频和第三方脚本。
- 通用部署移除固定 NAS IP、UID/GID、CPU 编号和本机目录假设。
- 核对开源许可证、依赖与脚本授权；本预览不代替许可决定。
- 提供真实脱敏截图、可验证安装步骤、测试说明和第三方来源边界。
- 配置贡献指南、问题模板和私密安全问题报告方式。
- 最后由用户检查并明确批准 public 开放。

## 依据

主项目当前 README、来源管理文档、任务执行与限流实现，以及用户在 2026-11-22 明确的产品方向。此阶段未重新验收生产环境。

- [Gitea 仓库镜像说明](https://docs.gitea.com/usage/repository/repo-mirror/)
- [GitHub 仓库许可说明](https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/customizing-your-repository/licensing-a-repository)
