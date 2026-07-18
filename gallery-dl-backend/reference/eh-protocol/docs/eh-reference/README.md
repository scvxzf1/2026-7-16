# EH / EHX 爬取与后续集成说明

本文整理当前项目在 E-Hentai（EH）与 ExHentai（ExH/EHX/EX）上的访问方式、
图片获取路径、配额及限速行为，以及后续接入下载器、任务队列或数据库时需要保留
的状态。

对应参考实现：[`scripts/eh_reference.py`](../../scripts/eh_reference.py)。

> 信息核对日期：2026-07-18。站点规则和返回页面可能调整；站点行为与本文不一致
> 时，应以实际响应和文末资料为准。

## 1. 范围

参考实现目前包含：

1. 按精确 `artist:` 标签发现画廊；
2. 下载平常图片查看器使用的 resample 图片；
3. 逐张获取 `fullimg` 原图，全部成功后在本地创建 ZIP；
4. 自动处理普通单页查看器与 MPV 查看器；
5. 支持 `e-hentai.org`、`exhentai.org`，以及作为 EH 别名输入的
   `g.e-hentai.org`；
6. 检测 GP 页面、图片额度 `509.gif`、失效图片链接和部分临时封锁响应。

当前没有实现 EH 的 **Archive Download** 服务。`original-zip` 是“逐张下载原图
后本地打包”，不是向 EH 请求站点生成的归档文件。

## 2. 术语和两个互相独立的维度

### 2.1 普通查看器与 MPV

它们是两种**页面及取图协议**，不是两种画质：

```text
画廊 /g/{gid}/{gallery_token}/
├── 普通单页查看器 /s/{image_token}/{gid}-{num}
│   └── 后续图片：POST /api.php，method=showpage
└── MPV /mpv/{gid}/{gallery_token}/
    └── 每张图片：POST /api.php，method=imagedispatch
```

MPV 即 Multi-Page Viewer。站点将多张图片连续加载；它可能要求 MPV Hath
Perk 或 Gold Star。集成层通常不需要向用户暴露此选项，解析器自动识别即可。

### 2.2 Resample 与 Original

它们是图片质量选择，与查看器协议正交：

| 页面/接口 | Resample | 逐张 Original |
|---|---|---|
| 普通首张图片页 | `<img id="img" src>` | `/fullimg...` |
| `showpage` | `i3` 中的图片 URL | `i6` 中的 `/fullimg...` |
| `imagedispatch` | `info["i"]` | `root + info["lf"]` |

因此实际是“普通/MPV × resample/original”的组合，而不是只有两套完全独立的
解析器。

## 3. EH 与 EHX 的关系

工程上可以近似理解为：

```text
EH ⊂ ExH
```

- EH 是公开可见集合；
- ExH 是需要有效账号会话的、更大的受限集合；
- 两边重叠画廊通常共享同一个 `gid` 和 `gallery_token`；
- ExH 独有画廊切换到 EH 域名后可能显示不可用；
- 账号过滤、私有状态、删除状态和区域状态会影响实际可见集合，所以它并非永久、
  严格的数学超集关系。

同时搜索两边时，应使用以下键去重：

```python
gallery_key = (gid, gallery_token)
```

推荐策略：

- 匿名任务只搜索 EH；
- 有有效 EHX Cookie 时优先搜索 EHX；
- 需要核对两边差异时分别搜索，再按 `(gid, token)` 合并。

## 4. 认证与 Cookie

### 4.1 EH resample

公开 EH 画廊的 resample 图片可以匿名获取，不需要账号 Cookie。参考脚本会自动
设置：

```text
nw=1
```

它是跳过提示页的站点偏好 Cookie，不是登录凭据。匿名会话仍按匿名/IP 配额
计算。

### 4.2 EHX

访问 EHX 需要有效登录会话。HTTP 层最终仍通过 Cookie 维持会话；即使实现账号
密码登录，登录成功后也是使用服务端下发的 Cookie 继续请求。

常见账号 Cookie 包括：

```text
ipb_member_id
ipb_pass_hash
```

浏览器会话还可能包含 `igneous` 等其他字段。集成时应导入完整 Netscape
`cookies.txt`，而不是假定两个字段永远足够。

参考脚本只实现 Cookie 导入，没有实现账号密码登录：

```console
--cookie-file cookies.txt
```

也可以重复指定字段：

```console
--cookie ipb_member_id=VALUE --cookie ipb_pass_hash=VALUE
```

Cookie 文件等同于活动登录会话，应放在版本控制之外，并限制文件读取权限。

## 5. 配额和速率限制

“限速”至少包含三套不同机制，不应混为一个固定的每秒请求数。

### 5.1 Image Limit：默认按出口 IP

匿名 EH 和普通登录账号的图片查看额度默认按公网出口 IP 统计。登录 EHX 只代表
获得访问权限，并不会自动把图片额度切换为账号额度。

以下状态可以使用账号额度：

- Bronze Star 或更高等级；
- `More Pages` Hath Perk；
- 临时购买的 account-based quota。

默认 IP 模式下，同一出口下的程序、浏览器和其他设备会共同消耗额度：

```text
同一公网 IP
├── 下载进程 A
├── 下载进程 B
├── 浏览器
└── NAT/VPN 的其他用户
```

共享代理、VPN 或运营商 NAT 的额度可能已被其他用户消耗。

### 5.2 Image Limit 的恢复

额度不是固定整点或每日一次性刷新，而是持续恢复：

- 通常每分钟恢复约 3～5 个 image hits；
- 实际速度随服务器负载变化；
- 欠 1,000 hits 时，理论恢复时间约为 3.3～5.6 小时；
- My Home 可以使用 GP 手动重置。

达到额度时，图片地址通常会变成：

```text
https://exhentai.org/img/509.gif
https://ehgt.org/g/509.gif
```

参考脚本检测到它们后抛出 `ImageLimitError`。此时立即重复请求只会继续消耗请求
资源；推荐保存进度、停止该 IP 的图片任务，稍后恢复。

### 5.3 Resample 和原图的计费差异

- Resample 不消耗 GP，但会消耗图片查看额度；
- 逐张原图可能消耗更多 image hits、Full Image Quota 或 GP；
- EHWiki 给出的原图 image-limit 计量约为 `10 × 文件大小(MB)`，站点实际代码
  还可能加基础 hit；
- 原图请求返回包含 `requires GP` 的 HTML 时，参考脚本抛出
  `GPRequiredError`；
- 严格原图模式不会自动把 resample 图片装进原图 ZIP。

原图的具体免费条件、FIQ 与 GP 规则会随画廊时间和服务器状态变化，不应把某个
固定公式写死为永久规则。

### 5.4 请求频率、搜索和 API 限制

站点没有公布一套适用于所有端点的统一 RPS：

- 搜索请求官方 Wiki 建议至少间隔 3 秒；
- `gdata` 元数据 API 每次最多 25 个条目，通常连续 4～5 次后等待约 5 秒；
- 图片页、`showpage`、`imagedispatch` 和图片文件请求是不同端点；
- 短时间建立过多连接可能触发临时 IP 封锁；
- 登录会话还可能受到账号状态或会话级校验，因此工程上应同时把账号与 IP 视为
  调度维度。

参考脚本默认在所有请求之间随机等待 3～6 秒：

```text
--interval 3 --interval-max 6
```

这是保守的集成默认值，不代表站点承诺“每 3 秒一定可请求一次”。

### 5.5 推荐调度策略

初始集成建议：

| 项目 | 建议 |
|---|---|
| 单个 IP + 会话的图片并发 | 1 |
| 普通请求间隔 | 随机 3～6 秒 |
| 搜索间隔 | 不小于 3 秒 |
| HTTP 429 | 长退避后重试，避免立即循环 |
| HTTP 5xx | 指数退避并设置最大次数 |
| `509.gif` | 保存断点并暂停该 IP 的图片任务 |
| GP HTML | 标记任务需要 GP，不降级混入原图结果 |
| 临时 ban 页面 | 暂停该 IP/会话，不继续并发探测 |

后续若提高吞吐量，应该先记录实际端点、响应码、每分钟 hits 变化和共享 IP 情况，
再分别调整搜索、页面 API 与图片下载队列，避免只增加线程数。

## 6. 画廊发现路径

### 6.1 按画师精确搜索

画师名通过 `artist:` namespace 搜索。

单词画师：

```text
artist:name$
```

带空格画师：

```text
artist:"artist name$"
```

请求形式：

```http
GET https://e-hentai.org/?f_search=artist%3A%22artist+name%24%22&page=0
```

搜索器执行以下流程：

1. 构造精确 `artist:` 查询；
2. 从结果页提取 `/g/{gid}/{token}/`；
3. 优先跟随页面中的 `nexturl`；
4. 旧式页面则增加 `page`；
5. 按 `(gid, token)` 去重。

当前实现是精确标签发现，不包含画师别名、罗马字变体或 tag alias 解析。后续可以
在 `search_artist()` 之前增加“输入名 → 多个候选标签”的别名层。

### 6.2 搜索结果的数据边界

搜索阶段只需要保存：

```python
{
    "site": "eh" or "exh",
    "gid": int,
    "token": str,
    "url": str,
}
```

不要在搜索阶段立即创建大量图片任务。先对 gallery key 去重，再进入画廊解析
队列，可以避免 EH/EHX 重复结果和分页重复链接。

## 7. 普通查看器的取图路径

普通画廊流程：

```text
GET /g/{gid}/{gallery_token}/
  ↓ 提取第一张 /s/ URL、标题、页数和 api_url
GET /s/{image_token}/{gid}-1
  ↓ 解析第一张图片、nextkey、startkey、showkey、nl
POST /api.php
  {
    "method": "showpage",
    "gid": gid,
    "page": num,
    "imgkey": nextkey,
    "showkey": showkey
  }
  ↓
重复直到 filecount
```

必须保留的链式状态：

```text
gid
gallery_token
当前页 num
当前 image_token/imgkey
下一页 nextkey
showkey
nl
api_url
```

`imgkey` 是链式更新的；跳过中间 API 调用直接请求后续页容易得到失效结果。

### 7.1 Resample 选择

- 第一张：图片页的 `<img id="img" src="...">`；
- 后续：`showpage` 返回的 `i3` 中的图片 URL。

### 7.2 Original 选择

- 第一张：图片页中的 `/fullimg...`；
- 后续：`showpage` 返回的 `i6` 中的 `/fullimg...`。

原图字段缺失时，严格原图任务应报告 `ParseError`，而不是悄悄使用 resample。

## 8. MPV 的取图路径

MPV 流程：

```text
GET /mpv/{gid}/{gallery_token}/
  ↓ 解析 JavaScript 变量 imagelist 和 mpvkey
对 imagelist 中每张图片：
POST /api.php
  {
    "method": "imagedispatch",
    "gid": gid,
    "page": num,
    "imgkey": image["k"],
    "mpvkey": mpvkey
  }
```

返回字段：

```text
info["i"]   resample URL
info["lf"]  原图相对路径
info["o"]   原图尺寸/大小信息
info["s"]   刷新 URL 使用的 nl 值
```

只有在 `info["o"]` 和 `info["lf"]` 表明原图可用时才应选择原图。当前会话没有
MPV 权限时，站点可能返回简短拒绝页；参考脚本将其转换成
`AuthenticationError`。

## 9. 图片 URL 刷新与有效期

最终的 H@H/CDN 图片 URL 不是适合长期持久化的主键。EHWiki 说明：

- H@H 集群图片 URL 最长通常约 15 分钟；
- 服务器直连 URL 最长通常约 24 小时。

数据库应保存画廊和图片页状态，而不是只保存最终图片 URL：

```text
(site, gid, gallery_token, num, image_token, viewer, mode, nl)
```

普通查看器 URL 刷新：

```text
GET /s/{image_token}/{gid}-{num}?nl={nl}
```

MPV resample URL 刷新：

```text
重新 POST imagedispatch，并附带 nl=info["s"]
```

逐张原图重试：

```text
{fullimg_url}?nl={nl}
```

重试次数应有限。持续拿到 `509.gif`、GP HTML 或临时 ban 页面时，不应使用
`nl` 进行无休止刷新。

## 10. 两条对外下载路径

### 10.1 Resample 目录下载

公开 EH 可以匿名执行：

```powershell
python scripts/eh_reference.py download `
    "https://e-hentai.org/g/GID/TOKEN/" `
    --mode resample --output downloads
```

程序流程：

```text
open_gallery()
→ iter_gallery_images(mode="resample")
→ download_image()
→ downloads/{gid title}/0001_filename.ext
```

### 10.2 逐张原图并在本地打包

```powershell
python scripts/eh_reference.py download `
    "https://exhentai.org/g/GID/TOKEN/" `
    --mode original-zip --output downloads `
    --cookie-file cookies.txt
```

程序流程：

```text
open_gallery()
→ iter_gallery_images(mode="original")
→ 临时目录逐张下载
→ 全部成功后创建 ZIP
→ 原子移动到最终输出路径
→ 删除临时目录
```

中途出现 GP、限额、认证或解析错误时，不生成一个看似完整的半成品 ZIP。

## 11. 可集成 API

```python
from scripts.eh_reference import EHClient

client = EHClient(
    "eh",
    cookie_file=None,
    interval=3,
    interval_max=6,
    retries=2,
    fallback_retries=2,
)
```

### 11.1 发现画廊

```python
for gallery in client.search_artist("artist name"):
    print(gallery.gid, gallery.token, gallery.url)
```

### 11.2 只使用 URL 枚举层

```python
gallery = client.open_gallery(
    "https://e-hentai.org/g/GID/TOKEN/",
)

for image in client.iter_gallery_images(gallery, mode="resample"):
    queue.put({
        "gid": image.gid,
        "num": image.num,
        "url": image.url,
        "image_token": image.image_token,
        "nl": image.nl,
    })
```

### 11.3 直接使用完整路径

```python
client.download_resampled(gallery.url, "downloads")
client.download_original_zip(gallery.url, "downloads")
```

主要替换边界：

- `EHClient._request()`：接入代理、全局限速器和观测指标；
- `EHClient.search_artist()`：接入数据库和画师别名系统；
- `EHClient.iter_gallery_images()`：保留站点协议，只输出任务；
- `EHClient.download_image()`：接入已有下载器或对象存储；
- `EHClient.download_original_zip()`：替换归档与发布流程。

## 12. 推荐的任务与数据库模型

### 12.1 画廊任务

```python
GalleryTask = {
    "site": "eh",               # eh / exh
    "gid": 123,
    "gallery_token": "0123456789",
    "mode": "resample",         # resample / original
    "next_num": 1,
    "status": "pending",
}
```

### 12.2 图片任务

```python
ImageTask = {
    "site": "eh",
    "gid": 123,
    "num": 1,
    "image_token": "0123456789",
    "viewer": "standard",       # standard / mpv
    "mode": "resample",
    "nl": "...",
    "url_obtained_at": "...",
    "status": "pending",
}
```

建议唯一键：

```text
画廊：(gid, gallery_token)
图片：(gid, num, mode)
```

同一个画廊的普通 `showpage` token 是顺序链，画廊解析任务适合串行；得到最终图片
任务后，下载队列仍建议按 IP/会话进行低并发调度。

## 13. 错误与调度动作

| 异常 | 含义 | 推荐动作 |
|---|---|---|
| `AuthenticationError` | EHX Cookie、MPV 权限或会话状态异常 | 刷新会话，暂停该账号任务 |
| `ParseError` | 页面字段变化、原图字段缺失或 token 错误 | 保存响应摘要，进入人工/版本检查 |
| `GPRequiredError` | 原图请求需要 GP/FIQ 确认 | 标记 `needs_gp`，不要混入 resample |
| `ImageLimitError` | IP/账号图片额度耗尽 | 保存断点，等待额度恢复 |
| `RequestError` | 网络、HTTP 或图片签名错误 | 有限退避重试，随后保留任务 |

日志中不要输出完整 Cookie、`ipb_pass_hash` 或其他会话字段。

## 14. 当前实现的验证状态

离线测试覆盖：

- EH/EHX 域名判断；
- `artist:` 查询构造与分页去重；
- 普通首图和 `showpage`；
- MPV `imagelist` 与 `imagedispatch`；
- resample/original 选择；
- `#pageN` 断点；
- GP HTML；
- 带查询参数的 `509.gif`；
- Content-Disposition 文件名；
- 原图全部完成后本地 ZIP。

运行：

```console
python scripts/run_tests.py eh_reference
```

当前共 13 个定向测试。另使用公开 EH 测试画廊做过匿名冒烟验证，成功枚举
4/4 张 resample URL，未实际落盘。

## 15. 资料来源

- [EHWiki：My Home / Image Limits](https://ehwiki.org/wiki/My_Settings)
- [EHWiki：Technical Issues](https://ehwiki.org/wiki/Technical_Issues)
- [EHWiki：Gallery Searching](https://ehwiki.org/wiki/Gallery_Searching)
- [EHWiki：API](https://ehwiki.org/wiki/API)
- [EHWiki：Downloading](https://ehwiki.org/wiki/Downloading)
- [EHWiki：Galleries / MPV](https://ehwiki.org/wiki/Galleries)
- [EHWiki：Gallery FAQ / 图片 URL 有效期](https://ehwiki.org/wiki/Gallery_FAQ)
- [gallery-dl EH/EHX extractor](https://github.com/mikf/gallery-dl/blob/master/gallery_dl/extractor/exhentai.py)
