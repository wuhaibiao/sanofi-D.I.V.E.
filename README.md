# Sanofi D I V E 记忆系统项目

本仓库以 `sanofi` 工作目录为顶层，保留记忆服务、技术方案、评测报告和流程图的目录结构。

| 目录或文件 | 内容 |
|---|---|
| [memsys](memsys/) | 记忆服务源码、测试、配置示例和部署脚本 |
| [技术方案初稿](memsys/docs/proposal-draft-20261029/长期交互式AI记忆系统技术方案初稿.docx) | 整体流程、技术选型、参考来源及21轮评测对照，使用原版流程图 |
| [历史评测报告](memsys/docs/evaluations/README.md) | 主干、实验分支与定向复测的已保留报告 |
| [.archify](.archify/) | 可交互流程图、图像与相关生成材料 |
| [graphify-out](graphify-out/) | 项目知识图谱与分析记录 |
| [DIVE 学生手册](DIVE-Student-Handbook.pdf) | 项目参考手册 |
| [原分析材料](DIVE-记忆系统分析.docx) | 记忆系统分析文档 |

服务安装与运行见 [memsys README](memsys/README.md) 和[本机部署说明](memsys/docs/local-deployment.md)。

本次代码快照来自本地 `memsys` 的 `codex/raw-chunk-rag` 实验分支，默认 `recall_mode` 仍为 `facts`。评测分数均保留原始标签，小样本回归不代表泛化验收。

本机密钥、运行配置、数据、原始运行日志、虚拟环境和工作区备份按忽略规则保留在本地。
