# 影视字幕本地化质检

一个仅使用 Python 标准库实现的字幕翻译、时间轴审核和交付服务。SQLite 保存项目、字幕版本、人员分配、时间点评论、术语表、复核意见和交付快照。

## 模块划分

- `store.py`：数据层，SQLite 存储、领域校验和状态机。
- `diffcalc.py`：差异计算，纯函数实现版本间句子级对比，可单独测试。
- `static/index.html`：页面，通过 HTTP 接口访问数据层。
- `app.py`：HTTP 路由与装配，不包含业务规则。

## 运行

```bash
python app.py --init
python app.py --port 8009
```

打开 <http://127.0.0.1:8009>。`--init` 会创建示例纪录片项目、`zh-CN` 草稿版本和一条术语规则。数据库默认是 `subtitle_qc.db`，可用 `--db` 或 `SUBTITLE_DB` 修改。旧库首次打开会自动补齐 `rework_reason` 列和返修基线表。

## 流程

1. 负责人创建项目、字幕版本和术语规则。
2. 为版本分配 `translator`、`timeline`、`reviewer`。
3. 翻译或时间轴成员保存字幕；每项包含 `expected_revision`，旧页面提交会返回 409。
4. 成员可对具体字幕或毫秒时间点添加评论。
5. 翻译/时间轴成员提交复核，分配的非创建人复核人批准或退回。
6. 负责人锁定已批准版本，再执行交付。
7. 交付时生成确定性的 SHA-256 快照；同语言的新交付会把旧版本标记为 `superseded`，但旧快照不会删除或覆盖。

## 返修

1. 负责人基于同项目、同语言的父版本创建新版本，必须填写返修原因；父版本不存在、属于其他项目或语言不一致时会说明具体原因并拒绝创建，不会留下带错误基线的版本。
2. 新版本自动继承父版本的字幕内容，创建时的父版本内容会快照为返修基线。
3. 打开新版本即可看到相对基线新增、改写和移除的句子；差异在复核通过前一直保留，每次打开按当前字幕重新计算。
4. 旧版本、原复核结论和已生成的交付快照始终完整可查。

字幕保存会验证时长范围、起点小于终点、字幕重叠、序号冲突和术语表。术语表中配置的禁用译法会直接阻止保存；指定译法可用。

## API

所有身份通过 `X-User`、`X-Role` 请求头模拟，角色包括 `owner`、`admin`、`translator`、`reviewer`、`timeline`。

- `POST /api/projects`：创建项目和成片校验信息。
- `POST /api/projects/{id}/versions`：创建目标语言版本，可指定同项目同语言的 `parent_id` 和必填的 `rework_reason`，并继承父版本字幕。
- `POST /api/projects/{id}/glossary`：设置指定译法和禁用词。
- `POST /api/versions/{id}/assignments`：分配角色。
- `POST /api/versions/{id}/cues`：新增或修改字幕，要求 `expected_revision`。
- `DELETE /api/versions/{id}/cues/{cue_id}`：删除草稿版本中的字幕。
- `POST /api/versions/{id}/comments`：按具体时间毫秒或字幕 ID 评论。
- `POST /api/versions/{id}/submit|review|lock|deliver`：完成审核交付状态机。
- `GET /api/versions/{id}/cues|comments|reviews`、`GET /api/deliveries`：查看结果。
- `GET /api/versions/{id}/diff`：查看相对父版本基线的新增、改写和移除句子。

## 测试

```bash
python -m unittest discover -s tests -v
```

测试覆盖完整复核交付流程、返修版本继承与差异、父版本基线校验、历史可查性、锁定覆盖保护、旧修订冲突、时间轴重叠、术语禁用和人员权限。
