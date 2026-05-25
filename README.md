# livephoto-worker

Docker 化的 Live Photo 合成 worker，适用于飞牛 NAS / Debian。它不会修改 Syncthing 源码；Syncthing 只需要把照片同步到 `/vol2/photos` 下对应输入目录即可。

## 功能

- 后台 worker 监听配置中的 `input_dir`。
- 发现同名图片和视频对后等待 `stable_seconds`，确认文件大小/mtime 稳定后处理。
- 调用 MotionPhoto2 合成为 Google Photos 可识别的 Motion Photo 单文件。
- 普通照片和普通视频不调用 MotionPhoto2，保留 EXIF/mtime 直接复制到 `output_dir`。
- `output_dir` 是 Pixel / Syncthing 唯一需要同步的整理后目录，包含 Motion Photo、普通照片和普通视频。
- 失败文件移动到 `failed_dir`。
- 可选将成功处理后的原始文件移动到 `archive_dir`。
- SQLite 记录已成功处理的文件内容指纹，避免重复合成。
- Web 设置页面：`http://NAS_IP:8011`。
- 日志输出到 stdout，Web 页面也显示最近日志。

## 默认配置

如果 `/config/config.json` 不存在，worker 启动时使用这些默认值：

```json
{
  "input_dir": "/photos/live_inbox",
  "output_dir": "/photos/motion_output",
  "archive_dir": "/photos/archive",
  "failed_dir": "/photos/failed",
  "stable_seconds": 30,
  "poll_interval": 10,
  "move_originals": true,
  "enable_archive": true,
  "recursive_scan": true,
  "preserve_directory_structure": true,
  "skip_dir_names": [
    ".stfolder",
    "@eaDir",
    "#recycle",
    ".Trash",
    ".AppleDouble",
    "__MACOSX"
  ]
}
```

Web 页面保存后会写入：

```text
/config/config.json
```

SQLite 数据库默认保存到：

```text
/config/livephoto-worker.sqlite3
```

## Web 页面

访问：

```text
http://NAS_IP:8011
```

页面提供：

- 保存配置
- 立即扫描一次
- 强制扫描并忽略稳定等待
- 查看最近日志
- 查看处理统计

可配置字段：

- `input_dir`
- `output_dir`
- `archive_dir`
- `failed_dir`
- `stable_seconds`
- `poll_interval`
- `move_originals`
- `enable_archive`
- `recursive_scan`
- `preserve_directory_structure`
- `skip_dir_names`

说明：

- `move_originals=false` 时，成功处理后原始图片和视频会留在输入目录。
- `enable_archive=false` 时，即使 `move_originals=true`，成功原始文件也不会移动到归档目录。
- `recursive_scan=true` 时递归扫描输入目录的多层子目录。
- `preserve_directory_structure=true` 时，输出、归档、失败目录会保留相对路径。
- `skip_dir_names` 每行一个目录名；系统会始终跳过 `.stfolder`、`@eaDir`、`#recycle`、`.Trash`、`.AppleDouble`、`__MACOSX`，并自动排除位于输入目录内的 output/archive/failed。
- 首次扫描大目录时，建议使用“强制扫描并忽略稳定等待”；日常增量同步建议使用默认稳定等待，避免处理未传输完成的文件。
- 失败文件仍会移动到 `failed_dir`，避免反复失败重试。

## Docker Compose 部署

`docker-compose.yml` 按要求只挂载两个目录：

```yaml
volumes:
  - /vol2/photos:/photos
  - /vol1/docker/livephoto-worker/config:/config
ports:
  - "8011:8011"
```

启动：

```bash
docker compose up -d --build
```

查看日志：

```bash
docker compose logs -f livephoto-worker
```

停止：

```bash
docker compose down
```

## 处理规则

- 默认递归扫描 `input_dir` 的所有子目录，也可在 Web UI 中关闭递归扫描。
- Live Photo 只在同一个目录内配对同名图片和视频，不跨目录配对，避免误配。
- 支持图片：`.HEIC` / `.HEIF` / `.JPG` / `.JPEG` / `.PNG`，大小写不敏感。
- 支持视频：`.MOV` / `.MP4` / `.M4V`，大小写不敏感。
- 如果同一 stem 同时有 HEIC 和 JPG，优先处理 HEIC。
- Motion Photo 输出默认沿用图片文件名，例如 `IMG_0001.HEIC`。
- 普通照片和普通视频会复制到 `output_dir`，原文件保留在输入目录。
- 已成功合并的 Live Photo 伴生 MOV 不会再作为普通视频复制到 `output_dir`。
- Pixel 只需要同步 `output_dir`，不要再同步原始输入目录。
- 开启“保留原目录结构”后，输出、归档、失败目录都会保留 input_dir 下的相对路径。
- 如果 output/archive/failed 目录位于 input_dir 内，扫描器会自动排除，防止重复处理。
- 递归扫描会跳过 `.stfolder`、`@eaDir`、`#recycle`、`.Trash`、`.AppleDouble`、`__MACOSX`。
- 如果输出或归档目录已有同名文件，会自动追加内容指纹短后缀，避免覆盖。
- SQLite 使用 SHA-256 内容指纹判断是否已成功处理过，避免重复处理。

## 本地测试

```bash
PYTHONPATH=src python3 -m unittest discover -s tests
```

## MotionPhoto2

Docker 构建阶段会下载 MotionPhoto2 Linux release（默认 `v2.7.7`）并安装 Debian 的 ExifTool 包。官方 Linux release 是 x86-64 ELF；飞牛 NAS 常见 x86_64 环境可用，ARM 设备需要另行准备可运行的 MotionPhoto2 并调整 `MOTIONPHOTO2_BIN`/Dockerfile。

MotionPhoto2 的 CLI 支持：

```bash
motionphoto2 --input-image ImageFile.HEIC --input-video VideoFile.MOV --output-file OutputFile.HEIC
```

## 参考

- [MotionPhoto2 README](https://github.com/PetrVys/MotionPhoto2#readme)
- [MotionPhoto2 v2.7.7 release](https://github.com/PetrVys/MotionPhoto2/releases/tag/v2.7.7)

## FPK 封装（LiveMotion）

本项目提供飞牛 NAS / fnOS FPK 应用封装，不影响 Docker Compose 直接部署方式。

> 当前结构已按 conversun/fnos-apps 发布的可安装 Docker FPK（AdGuardHome）对齐：FPK 根目录直接包含 `manifest`、`app.tgz`、`cmd/`、`config/`、`ui/`、`ICON.PNG`、`ICON_256.PNG` 等；Docker Compose 项目位于 `app.tgz` 内的 `docker/docker-compose.yaml`，而不是 FPK 根目录。该点与早期本项目模板不同。

### FPK 根目录结构

```text
LiveMotion.fpk
├── LiveMotion.sc
├── ICON.PNG
├── ICON_256.PNG
├── app.tgz                  # 内含 docker/ 与 ui/
├── cmd/
│   ├── common
│   ├── config_callback
│   ├── config_init
│   ├── install_callback
│   ├── install_init
│   ├── installer
│   ├── main
│   ├── service-setup
│   ├── uninstall_callback
│   ├── uninstall_init
│   ├── upgrade_callback
│   └── upgrade_init
├── config/
│   ├── privilege
│   └── resource
├── manifest
├── ui/
│   ├── config
│   └── images/
│       ├── 64.png
│       └── 256.png
└── wizard/
    └── config
```

`app.tgz` 内部结构：

```text
app.tgz
├── docker/
│   └── docker-compose.yaml       # 仅引用预构建镜像，不含 Dockerfile/源码
└── ui/
    ├── config
    └── images/
        ├── 64.png
        └── 256.png
```

### FPK 默认挂载和端口

`fpk/docker/docker-compose.yaml` 使用预构建镜像，不在飞牛本机 build：

```yaml
image: ghcr.io/dttxorg/livemotion:0.1.0
ports:
  - "8011:8011"
volumes:
  - /vol2/photos:/photos
  - /vol1/docker/livephoto-worker/config:/config
```

默认 Web UI 端口为 `8011`，安装后访问：

```text
http://NAS_IP:8011
```

### 修改默认挂载目录

如果你的飞牛 NAS 路径不同，修改：

```text
fpk/docker/docker-compose.yaml
fpk/cmd/service-setup
```

例如把照片目录从 `/vol2/photos` 改到 `/vol1/photos`：

```yaml
volumes:
  - /vol1/photos:/photos
  - /vol1/docker/livephoto-worker/config:/config
```

同时把生命周期脚本中的 `PHOTOS_DIR="/vol2/photos"` 改为你的实际路径。


### GHCR 发布流程

FPK 不再在飞牛 NAS 上执行 Docker build；飞牛只会拉取预构建镜像：

```text
ghcr.io/dttxorg/livemotion:0.1.0
```

已提供 GitHub Actions workflow：

```text
.github/workflows/docker-ghcr.yml
```

触发方式：

- push 到 `main` 自动 build & push
- 在 GitHub Actions 页面手动 `workflow_dispatch` 触发

推送 tag：

- `ghcr.io/dttxorg/livemotion:0.1.0`
- `ghcr.io/dttxorg/livemotion:latest`

#### 1. 创建 GitHub 仓库

1. 登录 GitHub。
2. 点击右上角 `+` → `New repository`。
3. `Owner` 选择 `dttxorg`。
4. `Repository name` 填写：`livemotion`。
5. 建议先不要勾选自动生成 README / .gitignore / license，避免和本地项目冲突。
6. 点击 `Create repository`。

仓库地址应为：

```text
https://github.com/dttxorg/livemotion
```

#### 2. 初始化 Git 并推送到 GitHub

在项目根目录执行：

```bash
git init
git add .
git commit -m "Initial LiveMotion release"
git branch -M main
git remote add origin git@github.com:dttxorg/livemotion.git
git push -u origin main
```

如果你使用 HTTPS 而不是 SSH，可以把 remote 改成：

```bash
git remote add origin https://github.com/dttxorg/livemotion.git
```

如果本地已经添加过 `origin`，则不要重复 `git remote add origin`，改用：

```bash
git remote set-url origin git@github.com:dttxorg/livemotion.git
```

#### 3. 查看 GitHub Actions 构建

推送完成后：

1. 打开 `https://github.com/dttxorg/livemotion`。
2. 点击仓库顶部的 `Actions`。
3. 找到 `Build and publish Docker image` workflow。
4. 打开最新一次运行，确认所有步骤成功。
5. 成功后 GHCR 应出现以下镜像 tag：
   - `ghcr.io/dttxorg/livemotion:0.1.0`
   - `ghcr.io/dttxorg/livemotion:latest`

也可以在 `Actions` 页面点击 `Run workflow` 手动触发一次构建。

#### 4. 查看 GHCR Packages

GitHub Actions 成功后：

1. 打开 `https://github.com/dttxorg`。
2. 点击 `Packages`。
3. 找到 `livemotion` package。
4. 打开 package 详情页，确认存在 `0.1.0` 和 `latest` tag。

#### 5. 将 GHCR Package 设置为 Public

飞牛 NAS 默认不适合依赖私有 GHCR 登录，因此建议把 package 设置为 Public：

1. 进入 `livemotion` package 详情页。
2. 点击右侧或顶部的 `Package settings`。
3. 找到 `Danger Zone` / `Change package visibility`。
4. 点击 `Change visibility`。
5. 选择 `Public`。
6. 按 GitHub 页面要求输入 package 名称进行确认。
7. 点击确认按钮完成修改。

截图说明文字可按下面方式标注：

```text
截图 1：在 GitHub 个人主页 dttxorg 的 Packages 页面中打开 livemotion。
截图 2：在 livemotion Package settings 页面找到 Change package visibility。
截图 3：选择 Public，并按页面提示输入 package 名称确认。
截图 4：Package 页面显示 Public 后，飞牛即可匿名 docker pull。
```

> GitHub 官方文档说明：Container registry 的 public package 可以匿名 pull；如果 package 保持 private，则需要在拉取端配置认证。注意：GitHub 页面会提示 package 一旦设为 Public，后续不能再改回 Private。

官方参考：

- [Working with the Container registry](https://docs.github.com/en/packages/working-with-a-github-packages-registry/working-with-the-container-registry)
- [Configuring a package's access control and visibility](https://docs.github.com/en/packages/learn-github-packages/configuring-a-packages-access-control-and-visibility)

#### 6. GitHub Personal Access Token 创建说明

GitHub Actions 使用仓库自带的 `GITHUB_TOKEN` 发布镜像，通常不需要额外创建 PAT。

只有在本地手动 `docker push` 到 GHCR 时，才需要创建 Personal Access Token。建议创建 classic token：

1. 打开 GitHub：`Settings` → `Developer settings` → `Personal access tokens` → `Tokens (classic)`。
2. 点击 `Generate new token`。
3. 选择过期时间。
4. 勾选权限：
   - `write:packages`
   - `read:packages`
5. 生成 token 后立即复制保存，例如保存为环境变量 `GHCR_TOKEN`。

```bash
export GHCR_TOKEN="粘贴你的 GitHub PAT"
```

> 注意：不要把 token 写入 git 仓库、README、脚本或截图中。

#### 7. 本地 Docker 手动发布步骤

如果不想等 GitHub Actions，或者需要手动验证镜像发布流程，可以在本地执行：

```bash
echo "$GHCR_TOKEN" | docker login ghcr.io -u dttxorg --password-stdin
docker build -t ghcr.io/dttxorg/livemotion:0.1.0 -t ghcr.io/dttxorg/livemotion:latest .
docker push ghcr.io/dttxorg/livemotion:0.1.0
docker push ghcr.io/dttxorg/livemotion:latest
```

#### 8. 验证镜像可拉取

本地验证：

```bash
docker pull ghcr.io/dttxorg/livemotion:0.1.0
```

飞牛 NAS / Debian 上验证：

```bash
docker pull ghcr.io/dttxorg/livemotion:0.1.0
```

#### 9. 重新生成并安装 FPK

镜像可拉取后，重新生成并校验 FPK：

```bash
./build-fpk.sh
./verify-fpk.sh dist/LiveMotion.fpk
```

然后在飞牛应用中心上传：

```text
dist/LiveMotion.fpk
```

### FPK 打包命令

在项目根目录执行：

```bash
./build-fpk.sh
```

输出：

```text
dist/LiveMotion.fpk
```

### 飞牛应用中心安装说明

1. 运行 `./build-fpk.sh` 生成 `dist/LiveMotion.fpk`。
2. 登录飞牛 NAS / fnOS 管理界面。
3. 进入应用中心的本地安装/手动安装入口。
4. 上传 `dist/LiveMotion.fpk`。
5. 安装完成后确认 Docker 容器 `livemotion` 正常运行。
6. 浏览器打开 `http://NAS_IP:8011`。
7. 在 Web UI 中确认或调整配置。

### FPK 校验

```bash
./verify-fpk.sh dist/LiveMotion.fpk
PYTHONPATH=src python3 -m unittest tests.test_fpk_package -v
```

完整测试：

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

## GHCR / 飞牛 pull 故障排查

如果飞牛无法 pull，请检查：

- package 是否 public
- image tag 是否存在
- Actions 是否成功
- compose 是否使用正确镜像名

正确镜像名应为：

```text
ghcr.io/dttxorg/livemotion:0.1.0
```
