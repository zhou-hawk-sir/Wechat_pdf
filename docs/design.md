# 聊天记录导出工具 — 设计文档

> 更新：2026-11-01
> 许可证：MIT

## 1. 目标

从本机微信 4.0 的本地数据中导出聊天记录（单聊 + 群聊），多种格式输出，供用户归档、备份、整理自己的数据。

库文件和媒体文件的格式都是拿本机真实文件逐个试出来的，导出样式和界面代码由本项目自己写。

## 2. 范围

- 仅支持微信 4.0（`xwechat_files` 目录结构），不支持 3.x。
- 会话类型：单聊与群聊。
- 导出格式：HTML、PDF、CSV、JSON、TXT。
- 媒体：图片、语音、视频、文件附件随导出还原并拷出。

明确不做：公众号会话、朋友圈、收藏、云同步、增量导出、3.x 兼容。

## 3. 技术栈

| 用途 | 选型 | 说明 |
|------|------|------|
| GUI | PySide6 | 单窗口三步式 |
| 加解密 | pycryptodome | SQLCipher 页解密、图片 `.dat` 解密 |
| 内存读取 | psutil + ctypes | 取数据库密钥 |
| 语音转码 | pilk | SILK → WAV（C 扩展） |
| 压缩内容 | zstandard | 消息 XML 解压 |
| HTML | Jinja2 | 导出模板 |
| PDF | 系统 Chrome / Edge 无头打印 | 不引入 GTK 依赖 |
| 测试 / 排版 | pytest / ruff | |
| 打包 | PyInstaller | 单文件 exe |

## 4. 目录结构

```
src/wechat_log/
├── __main__.py / app.py      # 入口
├── ui/
│   └── main_window.py        # 三步式主窗口
├── core/                     # 领域逻辑，不 import Qt
│   ├── locate.py             # 数据目录、账号、默认输出目录
│   ├── key.py                # 密钥获取（内存扫描 + 兜底 + 手动）
│   ├── decrypt.py            # SQLCipher 页解密 + WAL 合并
│   ├── imagekey.py           # 图片密钥推导、.dat 解密
│   ├── media.py              # 图片/语音/视频/文件还原
│   ├── database.py           # 读会话、联系人、消息
│   ├── models.py             # 数据类
│   └── service.py            # 编排：解密 → 读库 → 媒体 → 导出
└── export/                   # html / pdf / csv / json / txt
```

## 5. 数据流

1. **准备**（点一次按钮）：定位 `xwechat_files` → 扫描微信内存取密钥 → 识别当前登录账号 →
   解密 `session.db` 与 `contact.db` 读会话列表（几百 KB 级，秒级）。
2. **选择**：列表支持搜索、多选；勾选格式与「还原图片和语音」。
3. **导出**：按需解密 `message_*.db`，逐会话还原媒体并写出各格式文件；单会话失败不中断。

## 6. 关键实现

### 6.1 密钥

- 微信 4.2+ 把 `x'<64位hex密钥><32位hex盐>'` 字面量按 32 字节掩码异或后留在内存，扫描还原。
- 掩码是版本相关常量：默认内置一份；可用 `WECHAT_LOG_KEY_MASK` 覆盖（逗号分隔多个）。
- 掩码失配时用「已知盐反查」兜底：库文件前 16 字节即盐，按盐的 hex 串定位后向前取 64 位 hex。
- 校验：第 1 页 HMAC-SHA512 + 解密后 `SQLite format 3\0` 头。

### 6.2 解密

- 页 4096、保留区 80（IV16 + HMAC64），AES-256-CBC，第 1 页前 16 字节为盐。
- 微信运行时处于 WAL 模式，解密后会把 `-wal` 中已提交的帧合并进来，
  再用 `PRAGMA quick_check` 自检；自检失败则退回只看主库。

### 6.3 媒体

| 类型 | 位置 | 处理 |
|------|------|------|
| 图片 | `msg/attach/<dir1>/<dir2>/Img/*.dat` | `md5(uin+wxid)[:16]` → AES-128-ECB 头段 + 单字节 XOR 尾段；`wxgf` 用 ffmpeg 取首帧 |
| 语音 | `message/media_*.db` 的 `VoiceInfo` | SILK → WAV（pilk 缺失时保留 `.silk`） |
| 视频 | `msg/video/<dir>/<name>` | hardlink 的 `video_hardlink_info_v4` 按 md5 定位 |
| 文件 | `msg/file/<dir>/<name>` | hardlink 的 `file_hardlink_info_v4`，回退按原始文件名搜索 |

- 图片 `hardlink` 索引不全时，用「文件修改时间最接近消息时间」兜底，一个文件只占用一次。
- 视频/文件的目录取 `dir2id[dir1]`（月份目录），图片取 `dir2id[dir1]/dir2id[dir2]`。

### 6.4 导出

- HTML 与 PDF 共用同一份模板（气泡样式、封面、日期分隔），PDF 由 Chrome/Edge `--print-to-pdf` 生成。
- 媒体统一放在 `输出目录/媒体/<会话名>/`，文件名用消息时间戳，同名自动加序号；
  拷文件用 `copyfile`（不继承只读属性），因此重复导出到同一目录是幂等且安全的。

## 7. 错误处理

- 找不到数据目录 → 提示并支持手动指定。
- 取不到密钥 → 提示需管理员权限，并提供「导入密钥」。
- 单条媒体失败 → 只丢这一条，继续导出。
- PDF 缺浏览器 → 该项标记失败，其他格式不受影响。

## 8. 测试

- 单元测试：`decrypt`、`models`、`content`、`database`、`key`、`locate`、`exporters`、主窗口构造。
- 实机验证：对本机真实数据导出样例会话，核对条数、媒体数量与排版。

## 9. 风险

1. 密钥扫描依赖内存字面量布局，微信大版本升级可能失效 → 已提供掩码覆盖与盐反查兜底。
2. 图片密钥依赖 `uin` 与 `wxid` 的派生规则，规则变化会导致图片无法还原（语音不受影响）。
3. 媒体按需同步，本机没下载过的内容无法还原。
