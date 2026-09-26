# 黄金预期差复盘服务

政策预期、实际决定、市场价格与资金流数据按事件时间和可见时间保存。

服务通过 HTTP 接口交换业务事件，并使用 SQLite 文件保存本地状态。监听端口由 `PORT` 指定，数据文件位置由 `DATABASE_PATH` 指定；`contracts/entities.json` 记录首批稳定字段，`fixtures/example.json` 提供不含真实身份信息的示例。

## 数据授权与禁发控制

新闻生产的数据使用权与发布边界由同一服务管理：记录来源合同版本、可用栏目/地区/渠道、署名格式、原始素材与衍生图表关系、决议禁发时点与例外批准；编辑创建素材时即可获得针对计划发布时间与发布渠道的可用性判断。服务**只管理使用权和发布边界，不给出行情结论或交易建议**。详见 `docs/domain.md`，字段契约见 `contracts/licensing.json`，端到端示例见 `fixtures/licensing_example.json`。

### 接口

| 方法与路径 | 用途 |
| --- | --- |
| `POST /sources` · `POST /sources/{ref}/withdraw` | 登记来源（终端/公开声明/研究机构/官方）；来源撤回并级联复审 |
| `POST /contracts` · `GET /contracts/{ref}` · `GET /contracts/{ref}/usage` | 合同版本登记（只增不改，收窄触发级联）；额度占用查询 |
| `POST /materials` · `GET /materials/{ref}` · `POST /materials/{ref}/withdraw` | 素材登记，保留原时区、单位与取得时间；可在创建时携带 `review` 立即判断可用性 |
| `POST /derivatives` · `GET /derivatives/{ref}` · `POST /derivatives/{ref}/editors` | 衍生图表与上游素材关系；多人编辑不占用额度 |
| `POST /embargoes` · `POST /embargoes/{ref}/approvals` | 决议禁发时点与法务例外批准 |
| `POST /reviews` · `GET /reviews/{ref}` | 对 素材/衍生图表 × 发布时间/渠道/栏目/地区 的审查（只增不改） |
| `POST /articles` · `POST /articles/{ref}/versions` | 文章与不可变版本（版本固化实际引用的素材） |
| `POST /articles/{ref}/versions/{n}/reschedule` | 发布时间变动，生成新审查结果 |
| `POST /vouchers` · `GET /vouchers/{ref}` | 法务放行凭证，固定签发时规则；规则变化后凭证失效 |
| `POST /articles/{ref}/versions/{n}/publish` | 凭有效凭证发布；发布时最终核算授权额度 |
| `GET /articles/{ref}/compliance` | 许可依据、应展示署名、尚未闭环的渠道处置 |
| `POST /remediations` · `GET /remediations` · `GET /remediations/{ref}` · `POST /remediations/{ref}/close` | 补署名 / 替换 / 下架处置单 |

## 本地开发

运行 `make migrate` 初始化数据文件，`make test` 执行现有自动化检查，`make run` 启动服务。也可以使用 `docker compose up --build` 构建并运行容器，宿主机端口通过 `APP_PORT` 调整。
