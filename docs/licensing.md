# 数据授权与禁发控制

新闻生产的数据授权后端：记录来源合同版本、可用栏目/地区/渠道、署名格式、
原始数据与衍生图表的谱系、决议禁发时点与例外批准。编辑创建素材（文章版本）时
即获得针对**计划发布时间 × 发布渠道 × 地区**的可用性判断。

服务只管理使用权与发布边界，**不产生行情结论或交易建议**。

## 核心原则

1. **追加式事实**：合同收窄、来源撤回、禁发改期都不覆盖历史。
   - 合同新版本把旧版本置为 `superseded`；
   - 来源/素材/图表撤回只改状态并记录事件；
   - 禁发改期插入新行并把旧行置为 `superseded`，解除为 `lifted`；
   - 每次变化都产生**新的审查轮次**，旧审查结论永久可查。
2. **创建即判断**：创建文章版本即返回 verdict（`usable`/`blocked`）、
   逐渠道×地区决策、阻断原因（findings）与应展示署名。另有 `internal_review`
   内部审稿渠道的信息性结论，但它绝不构成对外发布授权。
3. **只有实际引用占额度**：授权额度按"被文章当前版本实际引用的素材/图表"计数，
   同一素材在多渠道多地区只占一个额度；多人编辑中未被版本引用的草稿不占额度。
   新版本提交后，同文章旧版本占用自动释放。
4. **凭证固定规则**：法务仅能对 verdict=usable 的 committed 审查签发凭证，
   凭证内嵌当时完整规则快照与 `snapshot_digest`；此后规则变化不影响凭证。
5. **发布后只能走处置**：已发布页面不被规则变化改写，一律进入
   `add_attribution`（补署名）/ `replace`（替换）/ `take_down`（下架）工单，
   文章报告列出所有未闭环（非 closed）的渠道处置。

## 时间与计量约定

- 所有时间为带偏移量 ISO 8601；现货、期货、利率、油价、官方购金素材各自保留
  原时区（`tz_name`）、原计量单位（`unit`）、事件时间（`event_time`）与取得时记录。
- 禁发判定按**计划发布时刻**与 `embargo_until` 比较（先转瞬时点，跨时区安全）。
- 例外到期同样按计划发布时刻判定；撤销按审查当下判定。
- 附件只存受控引用（`controlled_ref`）或 `sha256` 摘要（`payload_digest`/`spec_digest`）。

## 阻断与义务代码（findings.code）

| code | 含义 |
|---|---|
| `source_withdrawn` / `material_withdrawn` / `chart_withdrawn` | 来源/素材/图表已撤回 |
| `contract_inactive` | 评估所依据的来源当前合同已失效 |
| `section_denied` / `region_denied` / `channel_denied` | 栏目/地区/渠道不在授权范围 |
| `derived_not_allowed` | 合同不允许制作衍生图表 |
| `derived_upstream_denied` | 图表的上游素材在该渠道/地区无授权（链式阻断） |
| `embargo_active` | 计划发布时间早于禁发时点且无例外覆盖 |
| `quota_exceeded` | 来源授权额度不足 |
| `attribution_required` | 义务（非阻断）：须展示的署名格式 |
| `embargo_exception` | 信息：禁发被有效例外覆盖 |
| `internal_review_only` | 信息：内部审稿预览，非发布授权 |

## HTTP 接口

所有接口为 JSON over HTTP；POST 请求体为 JSON 对象，语义错误返回 400
`{"error": ...}`。`actor` 字段记录操作人（默认 `editor`/`legal`）。

### 来源与合同

- `POST /sources` `{source_id?, name, kind}` — kind: terminal/public_statement/research_pack/official
- `GET  /sources/{id}`
- `POST /sources/{id}/withdraw`
- `POST /sources/{id}/contracts` — 新建合同版本（同来源旧 current 版本自动 superseded）
  `{version_label, valid_from, valid_to?, allowed_sections?, allowed_regions?,`
  `allowed_channels?, attribution_template, derived_allowed?,`
  `derived_attribution_required?, quota_limit?, terms_digest?}`
  （范围列表用 `"*"` 表示全部）
- `GET  /contracts/{id}`

### 素材与图表

- `POST /materials` `{source_id, series_ref, category, event_time, tz_name, unit,`
  `controlled_ref?, payload_digest?, contract_version_id?}`
  category: `spot|futures|rate|oil|official_gold`；不指定合同版本则锁定来源当前版本
- `GET  /materials/{id}`，`POST /materials/{id}/withdraw`
- `POST /charts` `{material_id, title, upstream_material_ids?, spec_digest?}`
- `GET  /charts/{id}`，`POST /charts/{id}/withdraw`

### 禁运与例外

- `POST /embargoes` `{scope_type, scope_ref, embargo_until, reason}`
  scope_type: `global|category|source|material|chart`
- `POST /embargoes/{id}/reschedule` `{new_until}` — 旧行 superseded，返回新禁运行
- `POST /embargoes/{id}/lift`
- `POST /exceptions` `{target_type, target_id?, embargo_id?, reason, expires_at?}`
- `POST /exceptions/{id}/revoke`

### 文章、审查、凭证、发布、处置

- `POST /articles` `{title, section}`
- `POST /articles/{id}/versions` `{planned_publish_at, planned_channels,`
  `planned_regions, refs:[{material_id}|{chart_id}], commit?}`
  返回 review：`verdict, decisions[], findings[], attributions{}, rule_snapshot`。
  `commit:true` 且通过时占用额度、成为当前版本；不通过则保留为 preview。
- `GET  /reviews/{id}`
- `POST /reviews/{id}/credential` — 法务签发，凭证固定当时规则
- `GET  /credentials/{id}`
- `POST /publish` `{credential_id, channels?, regions?}` — 限凭证范围，重复发布幂等
- `POST /publications/{id}/dispositions` `{action_type, reason, assignee?}`
- `POST /dispositions/{id}` `{status: open|in_progress|closed, note?}`
- `GET  /articles/{id}/report` — 许可依据、应展示署名、发布渠道、未闭环处置

## 典型流程（美联储决议夜）

1. 终端合同登记栏目 `finance/macro`、地区 `CN/HK`、渠道 `web/app`（不含社媒）、
   署名模板、衍生授权与额度；研究机构限时材料登记更窄范围与禁发时点。
2. 编辑取得现货/期货/利率/油价/官方购金素材，各自保留原时区与单位。
3. 创建文章版本（计划 22:00、web+app、CN/HK）→ 立刻收到 blocked：
   禁发未到点、社媒渠道未授权、某衍生图缺署名等，全部在发稿前暴露。
4. 法务可对特定素材/图表授予限时例外；改计划时间或拿到例外后重新建版本→ usable。
5. 提交版本 → 法务凭证（固定规则）→ 按凭证渠道发布。
6. 决议后若来源撤回/合同收窄：新的版本审查自动 blocked；已发布页面生成
   补署名/替换/下架工单，文章报告持续跟踪至全部 closed。
