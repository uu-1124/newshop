MinIO 运行镜像以现有 Temurin 17 JRE 为基础，包含 MinIO Linux amd64 二进制。

- 版本：`RELEASE.2025-07-23T15-54-02Z`
- 二进制 SHA256：`eef6581f6509f43ece007a6f2eb4c5e3ce41498c8956e919a7ac7b4b170fa431`
- 当前服务器二进制：`/opt/mall-swarm/minio-runtime/minio`
- 当前镜像：`mall-swarm/minio:RELEASE.2025-07-23T15-54-02Z`

该运行文件不进入源码 Git。需要重建时，将服务器上的同一二进制保存到本目录，核对哈希后用 Dockerfile 构建；业务数据保存在 Compose 的 `minio-data` 卷，不在镜像中。当前实例已经验证上传、下载与删除。
