# agent-code-review

一句话：**本地 diff 进，带准确行号的 findings.json 出**——LLM 只负责说"哪段代码有问题"，行号由本地匹配算出来。

## 架构

```mermaid
flowchart LR
    A["git diff base...head<br/>adapter/git_diff.py"] --> B["core/diff_parser.py<br/>unidiff → ChangeSet"]
    B --> C["core/graph.py 节点1<br/>read_diff"]
    C --> D["core/graph.py 节点2<br/>review (LLM)"]
    D -->|"existing_code + message + severity<br/>（不含行号）"| E["findings/locate.py<br/>+ core/resolver.py 1级定位"]
    E --> F["findings.json<br/>file + start_line + end_line"]
    G["findings/schema.py<br/>ChangeSet / Finding 契约"] -.-> B
    G -.-> F
```

## 为什么不直接让模型输出行号（护城河）

让模型报行号看起来省事，实际是最贵的选择：

1. **模型数不准行号。** LLM 在 diff 里数行号是常见的系统性错误——尤其 hunk 头、删除行、
   多 hunk 文件，错位往往只差一两行，但 diff review 里"差一行"就等于指错了代码，
   评审人会直接失去信任。
2. **行号错了无法自检。** 模型给的 `line 12` 没有任何可验证性；而 `existing_code`
   是一段真实代码，**能不能在文件里找到它本身就是可验证的**——找不到就丢弃，
   不会产生假阳性定位。
3. **契约更稳。** 模型只输出 `{existing_code, message, severity}`，输出越短越不容易跑偏，
   也更容易被评测集（ReviewBench）统计。
4. **行号是本地可计算的确定性工作。** 用 unidiff 拿到 hunk 的新侧行号，
   在 hunk 内做逐行匹配即可，不需要模型参与——这正是 `core/resolver.py` 的职责，
   也是后续可以逐级加强（跨 hunk → 模糊匹配）的地方。

换句话说：**模型负责语义，本地负责坐标。** 二者解耦后，定位率就成了可度量、可优化的指标，
而不是玄学。

## 定位分级

| 级别 | 策略 | 状态 |
| --- | --- | --- |
| 1 | 同一 hunk 内逐行匹配（空白归一化） | ✅ 本阶段实现 |
| 2 | 跨 hunk / 全文件匹配 | 阶段二 |
| 3 | 模糊匹配 + 相似度阈值 | 阶段二 |

定位失败的结论会被**丢弃**并记入 dropped（`--dump-dropped` 可导出），宁可不报也不错报。

已知限制（1 级）：模型若只抄了半行（例如漏掉行尾注释），整行匹配会失败并丢弃该结论。
这正是 2/3 级定位要解决的召回率问题。

## 用法

```bash
python -m venv .venv                 # 建议用虚拟环境，依赖装在哪由你来定
. .venv/bin/activate                 # Windows: .venv\Scripts\activate
pip install -r requirements.txt
export DEEPSEEK_API_KEY=...          # 或 LLM_API_KEY / OPENAI_API_KEY
python cli.py --base main --head test-branch
```

可选参数：`--repo` `--out` `--model` `--base-url` `--max-files` `--locate-level` `--dump-dropped`。

LLM 走 OpenAI 兼容接口，默认 `https://api.deepseek.com/v1` + `deepseek-chat`。

> 依赖一律来自 `pip install -r requirements.txt` 装到的环境；代码里没有任何 `sys.path`
> 注入，也不读仓库内的依赖目录（确保 clone 后行为一致）。

## 测试

```bash
python -m unittest discover -s tests -t .
```

覆盖：契约 JSON 往返、diff 解析行号、1 级定位（含失败与越级）、locate 排序/丢弃、
以及临时 git 仓库上的端到端链路（用假 LLM 客户端，不联网）。

## MVP 的边界（本阶段刻意不做）

- **只支持本地 diff**：`adapter/github_pr.py` 只是占位，不接 GitHub API、不接 PR 评论。
- **只有 2 个节点**：`read_diff → review`。不做多智能体、不做角色拆分、不做自检循环。
- **不做评估**：`eval/metrics.py`、`eval/golden_loader.py` 是占位，尚未对接 ReviewBench。
- **只实现 1 级定位**：跨 hunk / 模糊匹配留到阶段二。
- **不做去重、不做修复建议、不做历史学习**。
