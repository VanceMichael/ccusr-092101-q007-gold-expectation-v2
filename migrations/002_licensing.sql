-- 数据授权与禁发控制
-- 所有时间均为带偏移量的 ISO 8601 文本；匹配在应用层完成，避免不同时区字符串直接比较。
-- 合同、禁运、审查、凭证均为追加式事实；收窄/撤回/改期通过新版本或新行表达，不覆盖历史。

-- 数据来源（行情终端 / 公开声明 / 研究机构限时材料 / 官方购金材料）
CREATE TABLE IF NOT EXISTS sources (
    source_id    TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    kind         TEXT NOT NULL CHECK (kind IN ('terminal', 'public_statement', 'research_pack', 'official')),
    status       TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'withdrawn')),
    withdrawn_at TEXT,
    created_at   TEXT NOT NULL
);

-- 来源合同版本：栏目 / 地区 / 渠道 / 署名 / 衍生授权 / 额度
CREATE TABLE IF NOT EXISTS contract_versions (
    contract_version_id         TEXT PRIMARY KEY,
    source_id                   TEXT NOT NULL REFERENCES sources(source_id),
    version_label               TEXT NOT NULL,
    status                      TEXT NOT NULL DEFAULT 'current'
                                    CHECK (status IN ('current', 'superseded')),
    valid_from                  TEXT NOT NULL,
    valid_to                    TEXT,
    allowed_sections            TEXT NOT NULL DEFAULT '["*"]',
    allowed_regions             TEXT NOT NULL DEFAULT '["*"]',
    allowed_channels            TEXT NOT NULL DEFAULT '["*"]',
    attribution_template        TEXT NOT NULL,
    derived_allowed             INTEGER NOT NULL DEFAULT 0,
    derived_attribution_required INTEGER NOT NULL DEFAULT 1,
    quota_limit                 INTEGER,
    terms_digest                TEXT NOT NULL,
    created_at                  TEXT NOT NULL,
    UNIQUE (source_id, version_label)
);

-- 原始素材：保留原时区、计量单位、取得时间；正文只存受控引用与摘要
CREATE TABLE IF NOT EXISTS materials (
    material_id                 TEXT PRIMARY KEY,
    source_id                   TEXT NOT NULL REFERENCES sources(source_id),
    acquired_contract_version_id TEXT NOT NULL REFERENCES contract_versions(contract_version_id),
    series_ref                  TEXT NOT NULL,
    category                    TEXT NOT NULL
                                    CHECK (category IN ('spot', 'futures', 'rate', 'oil', 'official_gold')),
    event_time                  TEXT NOT NULL,
    tz_name                     TEXT NOT NULL,
    unit                        TEXT NOT NULL,
    controlled_ref              TEXT,
    payload_digest              TEXT,
    status                      TEXT NOT NULL DEFAULT 'active'
                                    CHECK (status IN ('active', 'withdrawn')),
    created_at                  TEXT NOT NULL
);

-- 衍生图表：与原始素材的谱系固定
CREATE TABLE IF NOT EXISTS charts (
    chart_id                    TEXT PRIMARY KEY,
    source_id                   TEXT NOT NULL REFERENCES sources(source_id),
    material_id                 TEXT NOT NULL REFERENCES materials(material_id),
    title                       TEXT NOT NULL,
    spec_digest                 TEXT,
    status                      TEXT NOT NULL DEFAULT 'active'
                                    CHECK (status IN ('active', 'withdrawn')),
    created_at                  TEXT NOT NULL
);

-- 图表可由多个原始素材衍生（如现货+期货同图）；material_id 为主来源
CREATE TABLE IF NOT EXISTS chart_materials (
    chart_id    TEXT NOT NULL REFERENCES charts(chart_id),
    material_id TEXT NOT NULL REFERENCES materials(material_id),
    PRIMARY KEY (chart_id, material_id)
);

-- 决议禁发时点。改期=插入 supersede 旧行的新行；解除=插入 lifted 行，历史不覆盖。
CREATE TABLE IF NOT EXISTS embargoes (
    embargo_id      TEXT PRIMARY KEY,
    scope_type      TEXT NOT NULL CHECK (scope_type IN ('global', 'category', 'source', 'material', 'chart')),
    scope_ref       TEXT NOT NULL,
    embargo_until   TEXT NOT NULL,
    reason          TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'lifted', 'superseded')),
    supersedes_id   TEXT REFERENCES embargoes(embargo_id),
    created_by      TEXT NOT NULL,
    created_at      TEXT NOT NULL
);

-- 法务例外批准（可针对某条禁运、某个素材/图表，可设到期）
CREATE TABLE IF NOT EXISTS embargo_exceptions (
    exception_id    TEXT PRIMARY KEY,
    embargo_id      TEXT REFERENCES embargoes(embargo_id),
    target_type     TEXT NOT NULL CHECK (target_type IN ('material', 'chart', 'any')),
    target_id       TEXT NOT NULL DEFAULT '*',
    granted_by      TEXT NOT NULL,
    granted_at      TEXT NOT NULL,
    expires_at      TEXT,
    reason          TEXT NOT NULL,
    revoked_at      TEXT
);

-- 文章与版本
CREATE TABLE IF NOT EXISTS articles (
    article_id          TEXT PRIMARY KEY,
    title               TEXT NOT NULL,
    section             TEXT NOT NULL,
    current_version_id  TEXT,
    created_at          TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS article_versions (
    version_id          TEXT PRIMARY KEY,
    article_id          TEXT NOT NULL REFERENCES articles(article_id),
    version_seq         INTEGER NOT NULL,
    planned_publish_at  TEXT NOT NULL,
    planned_channels    TEXT NOT NULL,
    planned_regions     TEXT NOT NULL,
    created_by          TEXT NOT NULL,
    created_at          TEXT NOT NULL,
    UNIQUE (article_id, version_seq)
);

-- 被某一文章版本“实际引用”的素材/图表
CREATE TABLE IF NOT EXISTS version_refs (
    ref_id      TEXT PRIMARY KEY,
    version_id  TEXT NOT NULL REFERENCES article_versions(version_id),
    material_id TEXT REFERENCES materials(material_id),
    chart_id    TEXT REFERENCES charts(chart_id),
    quoted_at   TEXT NOT NULL,
    CHECK (material_id IS NOT NULL OR chart_id IS NOT NULL)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_version_refs_material
    ON version_refs(version_id, material_id) WHERE material_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_version_refs_chart
    ON version_refs(version_id, chart_id) WHERE chart_id IS NOT NULL;

-- 审查轮次：每次合同收窄/撤回/时点变动都产生新轮次，旧轮次保留
CREATE TABLE IF NOT EXISTS reviews (
    review_id           TEXT PRIMARY KEY,
    version_id          TEXT REFERENCES article_versions(version_id),
    kind                TEXT NOT NULL CHECK (kind IN ('preview', 'committed')),
    planned_publish_at  TEXT NOT NULL,
    planned_channels    TEXT NOT NULL,
    planned_regions     TEXT NOT NULL,
    section             TEXT NOT NULL,
    verdict             TEXT NOT NULL CHECK (verdict IN ('usable', 'blocked')),
    rule_snapshot       TEXT NOT NULL,
    created_by          TEXT NOT NULL,
    created_at          TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS review_decisions (
    review_id   TEXT NOT NULL REFERENCES reviews(review_id),
    channel     TEXT NOT NULL,
    region      TEXT NOT NULL,
    verdict     TEXT NOT NULL CHECK (verdict IN ('usable', 'blocked')),
    PRIMARY KEY (review_id, channel, region)
);

CREATE TABLE IF NOT EXISTS review_findings (
    finding_id  TEXT PRIMARY KEY,
    review_id   TEXT NOT NULL REFERENCES reviews(review_id),
    target_type TEXT CHECK (target_type IN ('material', 'chart')),
    target_id   TEXT,
    channel     TEXT,
    region      TEXT,
    code        TEXT NOT NULL,
    severity    TEXT NOT NULL CHECK (severity IN ('block', 'obligation', 'info')),
    message     TEXT NOT NULL
);

-- 授权额度占用：仅文章当前版本实际引用的素材占额；旧版本随提交自动释放。
-- 按来源计数：同一来源同时只有一个 current 合同，合同换版本后占用计数连续。
CREATE TABLE IF NOT EXISTS quota_holds (
    hold_id             TEXT PRIMARY KEY,
    source_id           TEXT NOT NULL REFERENCES sources(source_id),
    version_id          TEXT NOT NULL REFERENCES article_versions(version_id),
    target_type         TEXT NOT NULL CHECK (target_type IN ('material', 'chart')),
    target_id           TEXT NOT NULL,
    occupied_at         TEXT NOT NULL,
    UNIQUE (source_id, version_id, target_type, target_id)
);

-- 法务放行凭证：固定签发当时的全部规则快照，事后规则变化不改凭证
CREATE TABLE IF NOT EXISTS clearance_credentials (
    credential_id       TEXT PRIMARY KEY,
    review_id           TEXT NOT NULL UNIQUE REFERENCES reviews(review_id),
    version_id          TEXT NOT NULL REFERENCES article_versions(version_id),
    channels            TEXT NOT NULL,
    regions             TEXT NOT NULL,
    required_attributions TEXT NOT NULL,
    rule_snapshot       TEXT NOT NULL,
    snapshot_digest     TEXT NOT NULL,
    issued_by           TEXT NOT NULL,
    issued_at           TEXT NOT NULL
);

-- 渠道发布记录
CREATE TABLE IF NOT EXISTS publications (
    publication_id  TEXT PRIMARY KEY,
    article_id      TEXT NOT NULL REFERENCES articles(article_id),
    version_id      TEXT NOT NULL REFERENCES article_versions(version_id),
    credential_id   TEXT NOT NULL REFERENCES clearance_credentials(credential_id),
    channel         TEXT NOT NULL,
    region          TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'live'
                        CHECK (status IN ('live', 'corrected', 'replaced', 'taken_down')),
    published_at    TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    UNIQUE (version_id, channel, region)
);

-- 渠道处置：补署名 / 替换 / 下架；open 即“尚未闭环”
CREATE TABLE IF NOT EXISTS dispositions (
    disposition_id  TEXT PRIMARY KEY,
    publication_id  TEXT NOT NULL REFERENCES publications(publication_id),
    action_type     TEXT NOT NULL CHECK (action_type IN ('add_attribution', 'replace', 'take_down', 'note')),
    reason          TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'in_progress', 'closed')),
    assignee        TEXT,
    note            TEXT,
    opened_at       TEXT NOT NULL,
    closed_at       TEXT
);

-- 追加式业务事件台账：携带来源与序列号（领域约定）
CREATE TABLE IF NOT EXISTS event_journal (
    seq         INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type  TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    actor       TEXT NOT NULL,
    source_ref  TEXT NOT NULL,
    payload     TEXT NOT NULL
);

INSERT OR IGNORE INTO schema_migrations(version) VALUES ('002_licensing');
