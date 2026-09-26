# 影视字幕本地化质检

一个仅使用 Python 标准库实现的字幕翻译、时间轴审核和交付服务。SQLite 保存项目、字幕版本、人员分配、时间点评论、术语表、复核意见和交付快照。

数据层（`app.py` 的 `Database`）、差异计算（`diff.py` 纯函数）和页面（`static/` 下 HTML/CSS/JS）分开维护：页面和接口只渲染 `diff.diff_cues` 的结果，不自行实现比较逻辑。

## 运行

```bash
python app.py --init
python app.py --port 8009
```

打开 <http://127.0.0.1:8009>。`--init` 会创建示例纪录片项目、`zh-CN` 草稿版本和一条术语规则。数据库默认是 `subtitle_qc.db`，可用 `--db` 或 `SUBTITLE_DB` 修改。

## 流程

1. 负责人创建项目、字幕版本和术语规则。
2. 为版本分配 `translator`、`timeline`、`reviewer`。
3. 翻译或时间轴成员保存字幕；每项包含 `expected_revision`，旧页面提交会返回 409。
4. 成员可对具体字幕或毫秒时间点添加评论。
5. 翻译/时间轴成员提交复核，分配的非创建人复核人批准或退回。
6. 负责人锁定已批准版本，再执行交付。
7. 交付时生成确定性的 SHA-256 快照；同语言的新交付会把旧版本标记为 `superseded`，但旧快照不会删除或覆盖。

### 返修版本

- 复核退回后，负责人可创建新版本并选择**同项目同语言**的父版本，同时必须填写 `revision_reason`（返修原因）。
- 新版本创建时直接复制父版本的全部字幕，从既有内容继续修改；复核意见和评论保留在旧版本上，不随字幕复制。
- 打开新版本即展示相对父版本按字幕序号计算的差异：`added`（新增）、`modified`（改写，区分文案/时间轴）、`removed`（移除）、未变句子不计入。
- 复核通过前差异一直保留（`retained=true`）；批准后差异转为归档可查（`retained=false`），退回仍保持保留。
- 父版本校验失败时按原因分别报错：不存在、属于其他项目（提示项目名）、语言不一致（提示双方语言），不创建新版本。
- 旧版本、其原复核结论（`GET /api/versions/{id}/reviews`）和已生成交付快照（`GET /api/deliveries`）始终完整可查。

字幕保存会验证时长范围、起点小于终点、字幕重叠、序号冲突和术语表。术语表中配置的禁用译法会直接阻止保存；指定译法可用。

## API

所有身份通过 `X-User`、`X-Role` 请求头模拟，角色包括 `owner`、`admin`、`translator`、`reviewer`、`timeline`。

- `POST /api/projects`：创建项目和成片校验信息。
- `POST /api/projects/{id}/versions`：创建目标语言版本；指定 `parent_id` 时必须同项目同语言并带 `revision_reason`，新版本复制父版本字幕。
- `GET /api/versions?project_id=`、`GET /api/versions/{id}`：版本列表与详情（含父版本摘要和最新复核结论）。
- `GET /api/versions/{id}/diff`：返修版本相对父版本的新增/改写/移除差异及保留标记。
- `GET /api/versions/{id}/reviews`：该版本全部复核结论。
- `POST /api/projects/{id}/glossary`：设置指定译法和禁用词。
- `POST /api/versions/{id}/assignments`：分配角色。
- `POST /api/versions/{id}/cues`：新增或修改字幕，要求 `expected_revision`。
- `POST /api/versions/{id}/comments`：按具体时间毫秒或字幕 ID 评论。
- `POST /api/versions/{id}/submit|review|lock|deliver`：完成审核交付状态机。
- `GET /api/versions/{id}/cues|comments`、`GET /api/deliveries`：查看结果。

## 测试

```bash
python -m unittest discover -s tests -v
```

测试覆盖完整复核交付流程、锁定覆盖保护、旧修订冲突、时间轴重叠、术语禁用和人员权限，以及返修版本的字幕继承、父版本校验消息、差异计算与保留、旧版本/复核/快照留档。
