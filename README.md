# 美股热点扫描

基于 Yahoo Finance + GitHub Actions + pushplus 的免费美股量化扫描工具。

## 功能

每天美东时间 9:45 起，每 30 分钟扫描一次美股（NYSE + NASDAQ）：

1. 统计成交额 Top100 个股的行业分布
2. 按强度得分 S = (w/n) × ln(1+n) 排序取 Top5 行业
3. 每个行业合并取成交额 Top7 个股，通过 pushplus 推送到微信

## 数据源

- **行情**：Yahoo Finance chart API（免费，无需 API Key）
- **成分股**：Wikipedia S&P 500 + NASDAQ API
- **行业分类**：Wikipedia GICS 分类

## 运行

```bash
# 本地运行（需 Python 3.10+）
python main.py

# GitHub Actions 自动定时运行（见 .github/workflows/scan.yml）
```

## 推送

通过 pushplus 群组推送，在 `main.py` 顶部配置：
- `PUSHPLUS_TOKEN`：你的 pushplus token
- `PUSHPLUS_TOPIC`：pushplus 群组编码

## 定时时间（美东时间）

| 时间 | 说明 |
|------|------|
| 09:45 | 首次扫描 |
| 10:15 ~ 14:45 | 每30分钟一次 |

## 目录结构

```
us_stock_scanner/
├── main.py                    # 主程序
├── .github/workflows/scan.yml # GitHub Actions 定时任务
└── README.md
```

## 注意事项

- GitHub Actions 公开仓库免费无限使用
- 美股交易时间：美东 9:30-16:00（周一至周五）
- 数据有约15分钟延迟（Yahoo Finance 免费限制）
- 行业分类首次从 Wikipedia 获取，后续使用本地缓存
