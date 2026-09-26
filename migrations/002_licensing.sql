-- 数据授权与禁发控制：合同、素材、衍生图表、禁发、审查、文章版本、放行凭证与处置。
-- 所有时间字段均为带偏移量的 ISO 8601 字符串；外部主体一律使用引用编号。

CREATE TABLE IF NOT EXISTS sources (
    source_ref TEXT PRIMARY KEY,
    kind TEXT NOT NULL,                -- terminal | public_statement | research_institution | official | other
    display_name TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',   -- active | withdrawn
    withdrawn_at TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS contracts (
    contract_ref TEXT NOT NULL,
    version INTEGER NOT NULL,
    source_ref TEXT NOT NULL REFERENCES sources(source_ref),
    effective_from TEXT NOT NULL,
    effective_to TEXT,                 -- NULL 表示长期有效
    allowed_sections TEXT NOT NULL,    -- JSON 数组：可用栏目
    allowed_regions TEXT NOT NULL,     -- JSON 数组：可用地区
    allowed_channels TEXT NOT NULL,    -- JSON 数组：可用渠道
    attribution_required INTEGER NOT NULL DEFAULT 1,
    attribution_template TEXT NOT NULL DEFAULT '',  -- 署名格式，支持 {source} {acquired_at} {unit} {timezone}
    quota INTEGER,                     -- 授权额度：已发布版本实际引用的素材数上限；NULL 表示不限
    change_reason TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY (contract_ref, version)
);

CREATE TABLE IF NOT EXISTS materials (
    material_ref TEXT PRIMARY KEY,
    source_ref TEXT NOT NULL REFERENCES sources(source_ref),
    contract_ref TEXT NOT NULL,
    contract_version INTEGER NOT NULL,  -- 取得时固化的合同版本（溯源用；审查始终按最新版本评估）
    kind TEXT NOT NULL,                -- spot | futures | rates | oil | official_purchase
    acquired_at TEXT NOT NULL,         -- 取得时间，保留原时区偏移
    timezone TEXT,                     -- 原时区标签（如 America/New_York）
    unit TEXT NOT NULL,                -- 计量单位，原样保留
    payload_ref TEXT,                  -- 受控附件引用
    payload_sha256 TEXT,               -- 或附件摘要
    status TEXT NOT NULL DEFAULT 'available',   -- available | withdrawn
    withdrawn_at TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY (contract_ref, contract_version) REFERENCES contracts(contract_ref, version)
);

CREATE TABLE IF NOT EXISTS derivatives (
    derivative_ref TEXT PRIMARY KEY,
    attribution_text TEXT,             -- 衍生图表自带署名；为空且上游合同要求署名时审查不通过
    note TEXT,
    created_by TEXT,                   -- 编辑引用编号
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS derivative_parents (
    derivative_ref TEXT NOT NULL REFERENCES derivatives(derivative_ref),
    material_ref TEXT NOT NULL REFERENCES materials(material_ref),
    PRIMARY KEY (derivative_ref, material_ref)
);

-- 多人同时编辑同一图表只产生编辑记录，不占用授权额度
CREATE TABLE IF NOT EXISTS derivative_editors (
    derivative_ref TEXT NOT NULL REFERENCES derivatives(derivative_ref),
    editor_ref TEXT NOT NULL,
    joined_at TEXT NOT NULL,
    PRIMARY KEY (derivative_ref, editor_ref)
);

CREATE TABLE IF NOT EXISTS embargoes (
    embargo_ref TEXT PRIMARY KEY,
    event_ref TEXT NOT NULL,           -- 如 FOMC-2026-09
    source_ref TEXT,                   -- NULL 表示不限来源
    kind TEXT,                         -- NULL 表示不限素材类型
    channel TEXT,                      -- NULL 表示不限渠道
    region TEXT,                       -- NULL 表示不限地区
    embargo_until TEXT NOT NULL,       -- 决议禁发时点
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS embargo_approvals (
    approval_ref TEXT PRIMARY KEY,
    embargo_ref TEXT NOT NULL REFERENCES embargoes(embargo_ref),
    approver_ref TEXT NOT NULL,        -- 批准人引用编号
    channel TEXT,                      -- 例外放行范围；NULL 表示不限
    section TEXT,
    region TEXT,
    expires_at TEXT,                   -- 例外有效期；NULL 表示不限
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL
);

-- 审查结果只增不改：合同收窄、来源撤回、发布时间变动都只追加新记录
CREATE TABLE IF NOT EXISTS reviews (
    review_ref TEXT PRIMARY KEY,
    subject_type TEXT NOT NULL,        -- material | derivative
    subject_ref TEXT NOT NULL,
    planned_publish_at TEXT NOT NULL,
    channel TEXT NOT NULL,
    section TEXT NOT NULL,
    region TEXT NOT NULL,
    verdict TEXT NOT NULL,             -- allowed | blocked
    reasons TEXT NOT NULL,             -- JSON 数组：阻断原因代码
    attributions TEXT NOT NULL,        -- JSON 数组：应展示的署名
    rule_snapshot TEXT NOT NULL,       -- JSON：评估时固定的规则快照
    trigger TEXT NOT NULL,             -- editor_request | contract_narrowed | contract_updated |
                                       -- source_withdrawn | material_withdrawn | embargo_created |
                                       -- publish_time_changed | voucher_check
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_reviews_subject ON reviews(subject_type, subject_ref);

CREATE TABLE IF NOT EXISTS articles (
    article_ref TEXT PRIMARY KEY,
    desk TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS article_versions (
    article_ref TEXT NOT NULL REFERENCES articles(article_ref),
    version_no INTEGER NOT NULL,
    channels TEXT NOT NULL,            -- JSON 数组：计划发布渠道
    section TEXT NOT NULL,
    region TEXT NOT NULL,
    planned_publish_at TEXT NOT NULL,
    references_json TEXT NOT NULL,     -- JSON 数组：[{subject_type, subject_ref}]，版本内不可变
    status TEXT NOT NULL DEFAULT 'draft',   -- draft | published | superseded
    published_at TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY (article_ref, version_no)
);

-- 法务放行凭证：rule_snapshot 固定签发当时的规则，供事后审计与失效比对
CREATE TABLE IF NOT EXISTS vouchers (
    voucher_ref TEXT PRIMARY KEY,
    article_ref TEXT NOT NULL,
    version_no INTEGER NOT NULL,
    issued_by TEXT NOT NULL,           -- 法务引用编号
    rule_snapshot TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',   -- active | revoked
    issued_at TEXT NOT NULL
);

-- 已发布页面的渠道处置：补署名 add_attribution / 替换 replace / 下架 takedown
CREATE TABLE IF NOT EXISTS remediations (
    case_ref TEXT PRIMARY KEY,
    article_ref TEXT NOT NULL,
    version_no INTEGER,
    channel TEXT NOT NULL,
    subject_type TEXT,
    subject_ref TEXT,
    action_type TEXT NOT NULL,         -- add_attribution | replace | takedown
    reason TEXT NOT NULL,
    review_ref TEXT,
    status TEXT NOT NULL DEFAULT 'open',   -- open | closed
    opened_at TEXT NOT NULL,
    closed_at TEXT,
    resolution_note TEXT
);
CREATE INDEX IF NOT EXISTS idx_remediations_article ON remediations(article_ref, status);

INSERT OR IGNORE INTO schema_migrations(version) VALUES ('002_licensing');
