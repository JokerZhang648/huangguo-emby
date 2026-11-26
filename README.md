# 黄果短剧 · Emby 点播

NAS 定时读取黄果原创目录，通过本地 JSON 索引增量同步剧目和分集。Emby 从本地文件读取标题、简介和海报。点播时由服务取当集的播放地址，再跳转到 CDN。 [黄果原创](https://huangguoai.com/original/)

## 1. 放到 NAS 并启动

1. 克隆仓库或下载源代码，在 NAS 上放到 `/volume1/docker/huangguo-emby` 等项目目录。以下路径按你的存储空间替换。
2. 把 `bridge.env.example` 复制为 `bridge.env`，将其中的 `NAS_IP` 换成 NAS 的固定局域网 IP。`PUBLIC_BASE_URL` 会写入每集的播放文件，Emby 和播放设备都应能打开这个地址。NAS 通过代理出网时，可以在这个文件设置 `HTTPS_PROXY`。
3. 在群晖 Container Manager 里打开 **项目 → 新增**，将项目路径设为解压目录并使用其中的 `compose.yaml`，然后构建并启动项目。也可以通过 SSH 执行以下命令。首次构建需要 NAS 能访问 Docker Hub 和 PyPI。

```sh
cd /volume1/docker/huangguo-emby
sudo docker compose up -d --build
sudo docker compose logs -f bridge
```

如果旧版 DSM 的命令是 `docker-compose`，把命令中的 `docker compose` 换成 `docker-compose`。按 `Ctrl+C` 退出日志查看，容器继续运行。

4. 在浏览器打开 `http://NAS实际IP:8787/status`。`catalog_total` 是本轮读到的目录剧目数，`processed` 和 `to_process` 是详情队列的处理进度，`series_total` 是已入库剧目总数。`last_sync_bjt` 和 `next_sync_bjt` 以北京时间显示。

## 2. 添加 Emby 媒体库

1. 如果 Emby 跑在 Docker 中，把项目的 `media` 目录挂载给 **Emby 容器**。如果原有挂载已经包含这个目录，用它在容器内对应的路径即可。否则给 Emby 的 Compose 文件追加一条卷挂载，然后用现有项目配置重新部署 Emby。

   ```yaml
   volumes:
     - /volume1/docker/huangguo-emby/media/huangguo:/huangguo:ro
   ```

2. 在 Emby 服务端打开 **管理后台 → 媒体库 → 新增媒体库**。内容类型选 **电视节目**，名称填 **黄果短剧**。Docker 版填写容器内的目录（以上例子为 `/huangguo`），NAS 套件版填写服务进程可读的 NAS 路径（对应主机上的 `media/huangguo`）。
3. 在这个库的高级设置中启用 **元数据读取器 → Nfo**，关闭在线元数据和图片下载器，让标题和海报从本地文件读取。开启实时监控，或使用 Emby 的计划任务定期扫描。
4. 执行一次 **扫描媒体库文件**。看到海报后打开一部剧，检查第 1 集、切集和拖动进度条。以后新剧和新集会写到同一个媒体库目录。

## 3. 维护

- `HUANGGUO_CATALOG` 默认 `original`（黄果原创全部）；若改扫最近上新可设为 `newest`。`LATEST_COUNT` 默认 `0`（目录全部页）；若只需前 N 部可设为 `50` 等正整数。`SYNC_INTERVAL_MINUTES` 默认 `60`。修改 `bridge.env` 后运行 `sudo docker compose up -d --force-recreate` 应用配置。
- 想立即跑一轮同步，可以运行 `sudo docker compose restart bridge`。同步会优先检查目录列表靠前的剧，较早入库的连载也会定期补集。保留 `media` 和 `state` 两个目录即可保留已入库文件和同步索引。
- 如果 `/status` 显示 `degraded`，先看 `errors` 和容器日志。发生暂时性失败时会自动重试同步。
- 如果 `8787` 已被占用，可把 `compose.yaml` 中的映射改为 `8788:8787`，以及把 `PUBLIC_BASE_URL` 的端口改为 `8788`，再重建容器。播放地址只需在局域网内可访问。
- 如果 Emby 有封面但点播失败，先确认 Emby 机器能打开服务的 `/status`，再用容器日志区分播放接口错误和 CDN 错误。

## 4. 检查 NAS 到黄果的连接

如果 `/status` 显示「黄果原创页」或「最近上新页」连接失败，在 NAS 的项目目录运行下面的命令。它从正在运行的容器请求目录页，与同步流程使用同样的网络和请求头。

```sh
sudo docker compose exec -T bridge python bridge.py --check-upstream
```

看到「DNS 解析失败」时检查容器 DNS。出现 `ENETUNREACH` 或 `EHOSTUNREACH` 时，先用 NAS 宿主机访问同一网址，比较宿主机和容器的连通性。连接超时时，同时检查 NAS 的出口和黄果站点的响应。返回「可访问」后，可以运行 `sudo docker compose restart bridge` 立即重试同步。

更新 Python 源文件后，可用 `sudo docker compose up -d --no-build --pull never --force-recreate bridge` 复用已有镜像并加载代码。安装或更新 Python 依赖时，使用第 1 节的构建命令。

## 参考

 [黄果原创](https://huangguoai.com/original/) · [黄果最近上新](https://huangguoai.com/newest) · [群晖 Container Manager 项目](https://kb.synology.com/en-global/DSM/help/ContainerManager/docker_project) · [Emby 电视节目](https://www.emby.media/support/articles/TV-Naming.html) · [Emby STRM](https://www.emby.media/support/articles/Strm-Files.html)
