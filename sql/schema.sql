-- =====================================================================
-- LINE AI Bot 資料表結構（SQL Server）
--
-- 設計目標：
--   1) customers      客戶訊息：以 LINE userId 為主鍵，記錄暱稱等資料
--   2) purchases      購買訊息：客戶的購買紀錄
--   3) parts_prices   零件價格：零件報價表
--   4) conversations  （輔助）原始對話紀錄，供產生對話精華使用
--
-- 此腳本可重複執行（IF NOT EXISTS 保護），WebUI 的「初始化資料表」會呼叫它。
-- 請先建立並選擇好資料庫（例如 [庫存]）再執行。
-- =====================================================================

SET ANSI_NULLS ON;
SET QUOTED_IDENTIFIER ON;
GO

-- ---------------------------------------------------------------------
-- 客戶訊息
-- ---------------------------------------------------------------------
IF NOT EXISTS (SELECT 1 FROM sys.tables WHERE name = 'customers')
BEGIN
    CREATE TABLE dbo.customers (
        line_user_id     NVARCHAR(64)   NOT NULL PRIMARY KEY,  -- LINE userId（U 開頭）
        display_name     NVARCHAR(128)  NULL,                  -- LINE 暱稱
        picture_url      NVARCHAR(512)  NULL,                  -- 大頭貼網址
        status_message   NVARCHAR(512)  NULL,                  -- 狀態訊息
        language         NVARCHAR(16)   NULL,                  -- 語系
        is_friend        BIT            NOT NULL DEFAULT 1,    -- 是否為好友
        first_seen_at    DATETIME2(0)   NOT NULL DEFAULT SYSDATETIME(),
        last_seen_at     DATETIME2(0)   NOT NULL DEFAULT SYSDATETIME(),
        note             NVARCHAR(MAX)  NULL                   -- 自訂備註
    );
END;
GO

-- ---------------------------------------------------------------------
-- 購買訊息
-- ---------------------------------------------------------------------
IF NOT EXISTS (SELECT 1 FROM sys.tables WHERE name = 'purchases')
BEGIN
    CREATE TABLE dbo.purchases (
        id               BIGINT         IDENTITY(1,1) PRIMARY KEY,
        line_user_id     NVARCHAR(64)   NOT NULL,              -- 對應 customers.line_user_id
        part_no          NVARCHAR(64)   NULL,                  -- 零件編號（對應 parts_prices.part_no）
        part_name        NVARCHAR(256)  NULL,                  -- 零件名稱（下單當下快照）
        quantity         INT            NOT NULL DEFAULT 1,
        unit_price       DECIMAL(18,2)  NULL,                  -- 成交單價
        total_amount     DECIMAL(18,2)  NULL,                  -- 成交總額
        status           NVARCHAR(32)   NOT NULL DEFAULT N'待確認', -- 待確認/已成立/已出貨/已取消
        purchased_at     DATETIME2(0)   NOT NULL DEFAULT SYSDATETIME(),
        note             NVARCHAR(MAX)  NULL,
        CONSTRAINT FK_purchases_customers
            FOREIGN KEY (line_user_id) REFERENCES dbo.customers(line_user_id)
    );
    CREATE INDEX IX_purchases_user ON dbo.purchases(line_user_id);
    CREATE INDEX IX_purchases_part ON dbo.purchases(part_no);
END;
GO

-- ---------------------------------------------------------------------
-- 零件價格
-- ---------------------------------------------------------------------
IF NOT EXISTS (SELECT 1 FROM sys.tables WHERE name = 'parts_prices')
BEGIN
    CREATE TABLE dbo.parts_prices (
        part_no          NVARCHAR(64)   NOT NULL PRIMARY KEY,  -- 零件編號
        category         NVARCHAR(64)   NULL,                  -- 類別（顯示卡/處理器/筆電/桌機…）
        brand            NVARCHAR(64)   NULL,                  -- 品牌（華碩/技嘉/微星…）
        part_name        NVARCHAR(256)  NOT NULL,              -- 品名（零件名稱）
        spec             NVARCHAR(512)  NULL,                  -- 規格描述
        unit             NVARCHAR(32)   NULL,                  -- 計價單位（個/箱/公斤…）
        price            DECIMAL(18,2)  NOT NULL DEFAULT 0,    -- 單價
        currency         NVARCHAR(8)    NOT NULL DEFAULT N'TWD',
        stock_qty        INT            NULL,                  -- 庫存數量（選填）
        is_active        BIT            NOT NULL DEFAULT 1,    -- 是否上架
        updated_at       DATETIME2(0)   NOT NULL DEFAULT SYSDATETIME()
    );
    CREATE INDEX IX_parts_name ON dbo.parts_prices(part_name);
    CREATE INDEX IX_parts_category ON dbo.parts_prices(category);
END;
GO

-- 既有資料表升級：補上類別 / 品牌欄位（可重複執行）
IF COL_LENGTH('dbo.parts_prices', 'category') IS NULL
    ALTER TABLE dbo.parts_prices ADD category NVARCHAR(64) NULL;
GO
IF COL_LENGTH('dbo.parts_prices', 'brand') IS NULL
    ALTER TABLE dbo.parts_prices ADD brand NVARCHAR(64) NULL;
GO
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'IX_parts_category' AND object_id = OBJECT_ID('dbo.parts_prices'))
    CREATE INDEX IX_parts_category ON dbo.parts_prices(category);
GO

-- ---------------------------------------------------------------------
-- 對話紀錄（輔助：供產生對話精華 / 記憶）
-- ---------------------------------------------------------------------
IF NOT EXISTS (SELECT 1 FROM sys.tables WHERE name = 'conversations')
BEGIN
    CREATE TABLE dbo.conversations (
        id               BIGINT         IDENTITY(1,1) PRIMARY KEY,
        line_user_id     NVARCHAR(64)   NOT NULL,              -- 對應 customers.line_user_id
        role             NVARCHAR(16)   NOT NULL,              -- user / assistant
        content          NVARCHAR(MAX)  NOT NULL,              -- 訊息內容
        created_at       DATETIME2(0)   NOT NULL DEFAULT SYSDATETIME(),
        CONSTRAINT FK_conversations_customers
            FOREIGN KEY (line_user_id) REFERENCES dbo.customers(line_user_id)
    );
    CREATE INDEX IX_conv_user_time ON dbo.conversations(line_user_id, created_at);
END;
GO

-- ---------------------------------------------------------------------
-- 原價屋報價快取（每日大爬一次存這裡，AI 查詢直接讀此表，避免爬爛對方網站）
-- ---------------------------------------------------------------------
IF NOT EXISTS (SELECT 1 FROM sys.tables WHERE name = 'coolpc_cache')
BEGIN
    CREATE TABLE dbo.coolpc_cache (
        id            BIGINT         IDENTITY(1,1) PRIMARY KEY,
        category      NVARCHAR(128)  NULL,                 -- 類別（原價屋的 optgroup 標題）
        brand         NVARCHAR(64)   NULL,                 -- 品牌（由品名推斷，便於第二層分辨）
        item_name     NVARCHAR(512)  NOT NULL,             -- 品名（品項名稱，原始文字）
        price         DECIMAL(18,2)  NULL,                 -- 解析出的價格
        raw_text      NVARCHAR(1024) NULL,                 -- 原始整行文字（保留備查）
        updated_at    DATETIME2(0)   NOT NULL DEFAULT SYSDATETIME()
    );
    CREATE INDEX IX_coolpc_name ON dbo.coolpc_cache(item_name);
    CREATE INDEX IX_coolpc_category ON dbo.coolpc_cache(category);
END;
GO

-- 既有資料表升級：補上品牌欄位（可重複執行）
IF COL_LENGTH('dbo.coolpc_cache', 'brand') IS NULL
    ALTER TABLE dbo.coolpc_cache ADD brand NVARCHAR(64) NULL;
GO
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'IX_coolpc_category' AND object_id = OBJECT_ID('dbo.coolpc_cache'))
    CREATE INDEX IX_coolpc_category ON dbo.coolpc_cache(category);
GO
