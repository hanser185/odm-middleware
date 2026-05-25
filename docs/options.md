# ODM 处理选项说明文档

## 后处理选项

| 选项 | 默认值 | 说明 |
|------|--------|------|
| `end-with` | `odm_postprocess` | 指定处理流程的最后一个阶段。可选值：`odm_filterpoints`、`odm_meshing`、`odm_texturing`、`odm_georeferencing`、`odm_orthophoto`、`odm_postprocess` |
| `rerun-from` | - | 从指定阶段重新运行处理流程。可选值：`dataset`、`split`、`merge`、`opensfm`、`openmvs`、`odm_filterpoints`、`odm_meshing`、`odm_texturing`、`odm_georeferencing`、`odm_orthophoto`、`odm_postprocess` |

## 特征提取选项

| 选项 | 默认值 | 说明 |
|------|--------|------|
| `min-num-features` | `10000` | 每张图片提取的最小特征点数量。特征点越多，匹配越准确，但处理时间越长。适用于特征较少的场景（如水面、草地）。 |
| `feature-type` | `dspsift` | 特征提取算法类型。可选值：`sift`（标准SIFT）、`dspsift`（DNN辅助SIFT，更准确但更慢）、`orb`（快速但精度较低） |
| `feature-quality` | `high` | 特征提取质量。可选值：`ultra`（最高质量，最慢）、`high`（高质量）、`medium`（中等质量）、`low`（低质量，最快）、`lowest`（最低质量） |

## 特征匹配选项

| 选项 | 默认值 | 说明 |
|------|--------|------|
| `matcher-type` | `flann` | 特征匹配算法。可选值：`flann`（快速近似最近邻，适用于大多数场景）、`bruteforce`（暴力匹配，更准确但更慢）、`superglue`（深度学习匹配，适用于困难场景） |
| `matcher-neighbors` | `0` | 自动选择每张图片进行匹配的近邻数量。0表示自动计算（基于GPS信息）。适用于大型数据集以加速处理。 |
| `matcher-order` | `0` | 启用基于图像顺序的匹配，值为用于匹配的相邻图像数量。适用于视频帧或无人机拍摄的场景。 |

## 相机参数选项

| 选项 | 默认值 | 说明 |
|------|--------|------|
| `use-fixed-camera-params` | `false` | 是否使用固定的相机参数进行重建。启用后不会优化相机内参，适用于已知相机参数的场景。 |
| `cameras` | - | 手动指定相机参数JSON文件路径。格式：`{"focal":焦距,"width":宽度,"height":高度}` |
| `camera-lens` | `auto` | 相机镜头类型。可选值：`auto`（自动检测）、`perspective`（透视镜头）、`brown`（径向畸变镜头）、`fisheye`（鱼眼镜头）、`fisheye_opencv`、`spherical`（全景镜头） |

## 辐射校准选项

| 选项 | 默认值 | 说明 |
|------|--------|------|
| `radiometric-calibration` | `none` | 辐射校准类型，用于校正传感器和光照影响。可选值：`none`（不校准）、`cosine`（余弦校准）、`flatfield`（平场校准）、`cosine+flatfield`（联合校准） |

## 性能选项

| 选项 | 默认值 | 说明 |
|------|--------|------|
| `max-concurrency` | `8` | 处理过程中使用的最大CPU线程数。建议设置为CPU核心数。 |
| `use-hybrid-bundle-adjustment` | `false` | 是否使用混合光束法平差。启用后先在子集上优化，再在全集上优化，可加速大型场景处理。 |
| `sfm-algorithm` | `incremental` | 运动恢复结构（SfM）算法。可选值：`incremental`（增量式，逐张添加图像）、`sequential`（顺序式）、`parallel`（并行式）、`triangulation`（基于三角测量） |
| `sfm-no-partial` | `false` | 是否跳过部分重建。启用后如果部分图像无法重建则报错，而不是忽略无法重建的图像。 |

## AI 去除选项

| 选项 | 默认值 | 说明 |
|------|--------|------|
| `sky-removal` | `false` | 是否使用AI去除图像中的天空区域。适用于生成正射影像时去除不需要的天空。 |
| `bg-removal` | `false` | 是否使用AI去除背景。基于深度学习的前景检测，适用于物体重建场景。 |

## 三维网格选项

| 选项 | 默认值 | 说明 |
|------|--------|------|
| `use-3dmesh` | `false` | 是否生成三维网格（三维表面模型）。生成.ply格式的三维网格文件。 |
| `skip-3dmodel` | `false` | 是否跳过三维模型生成。启用后不生成三维网格，加快处理速度。 |
| `mesh-size` | `200000` | 三维网格的顶点数量。值越大，网格越精细，但处理时间越长。 |
| `mesh-octree-depth` | `11` | 三维网格重建时的八叉树深度。值越大，细节越丰富，但内存消耗越大。 |

## 报告选项

| 选项 | 默认值 | 说明 |
|------|--------|------|
| `skip-report` | `false` | 是否跳过生成报告。报告包含处理统计信息和质量评估。 |

## 正射影像选项

| 选项 | 默认值 | 说明 |
|------|--------|------|
| `skip-orthophoto` | `false` | 是否跳过正射影像生成。正射影像是经过几何校正的航拍影像。 |
| `ignore-gsd` | `false` | 是否忽略地面采样距离（GSD）检查。启用后即使GSD异常也继续处理。 |
| `fast-orthophoto` | `false` | 是否启用快速正射影像生成模式。降低分辨率以加快处理速度。 |
| `orthophoto-resolution` | `5` | 正射影像分辨率（厘米/像素）。值越小，分辨率越高，文件越大。 |
| `orthophoto-no-tiled` | `false` | 是否禁用分块输出。默认输出分块的GeoTIFF，启用后输出单个大块文件。 |
| `orthophoto-png` | `false` | 是否同时输出PNG格式的正射影像。除了默认的GeoTIFF外，额外生成PNG文件。 |
| `orthophoto-kmz` | `false` | 是否生成KMZ格式（带地理信息的ZIP压缩KML文件）。用于Google Earth查看。 |
| `orthophoto-compression` | `DEFLATE` | 正射影像压缩算法。可选值：`DEFLATE`（无损压缩）、`JPEG`（有损压缩，文件更小） |
| `orthophoto-cutline` | `false` | 是否生成正射影像裁切线。用于多区块拼接时的边界优化。 |

## 边界选项

| 选项 | 默认值 | 说明 |
|------|--------|------|
| `crop` | `3` | 从边界裁剪的米数。从处理区域边缘向内裁剪指定距离，去除边缘伪影。 |
| `boundary` | - | 自定义边界GeoJSON文件路径。指定处理区域的精确边界。 |
| `auto-boundary` | `false` | 是否自动生成边界。基于图像覆盖范围自动生成处理边界。 |
| `auto-boundary-distance` | `0` | 自动边界的扩展距离（米）。正值扩大边界，负值缩小边界。 |

## 点云选项

| 选项 | 默认值 | 说明 |
|------|--------|------|
| `pc-quality` | `medium` | 点云重建质量。可选值：`ultra`、`high`、`medium`、`low`、`lowest` |
| `pc-classify` | `false` | 是否对点云进行分类。识别地面、植被、建筑物等不同地物类型。 |
| `pc-csv` | `false` | 是否输出CSV格式的点云文件。文本格式，易于查看但文件较大。 |
| `pc-las` | `false` | 是否输出LAS格式的点云文件。标准LiDAR格式，兼容大多数GIS软件。 |
| `pc-ept` | `false` | 是否输出EPT格式的点云。Entwine点云格式，适用于Web可视化。 |
| `pc-copc` | `false` | 是否输出COPC格式的点云。分块立体点云格式，支持高效流式传输。 |
| `pc-filter` | `5` | 点云滤波半径（米）。用于去除离群点，值越大过滤越强。 |
| `pc-sample` | `0` | 点云采样间距。用于降低点云密度，0表示不采样。 |
| `pc-skip-geometric` | `false` | 是否跳过几何精化。启用后不进行点云几何优化，加快处理速度。 |
| `pc-rectify` | `false` | 是否校正点云。用于修正点云的倾斜问题。 |

## 地面滤波选项（SMRF）

| 选项 | 默认值 | 说明 |
|------|--------|------|
| `smrf-scalar` | `1.25` | 渐进形态学滤波（SMRF）的标量参数。控制滤波窗口大小，影响地面点分类。 |
| `smrf-slope` | `0.15` | SMRF的坡度阈值。用于区分地面和非地面点，值越大越宽松。 |
| `smrf-threshold` | `0.5` | SMRF的高度阈值（米）。高于此值的点被分类为非地面点。 |
| `smrf-window` | `18` | SMRF的窗口大小（米）。用于滤波分析的窗口尺寸。 |

## 纹理选项

| 选项 | 默认值 | 说明 |
|------|--------|------|
| `texturing-skip-global-seam-leveling` | `false` | 是否跳过全局接缝平整。启用后不进行纹理接缝优化，可能产生可见接缝。 |
| `texturing-keep-unseen-faces` | `false` | 是否保留不可见面。启用后保留没有被任何图像覆盖的网格面。 |
| `texturing-single-material` | `false` | 是否使用单一材质。启用后整个模型使用一个纹理，减少文件复杂度。 |
| `gltf` | `false` | 是否输出glTF格式。现代Web友好的三维格式，支持PBR材质。 |

## EXIF选项

| 选项 | 默认值 | 说明 |
|------|--------|------|
| `use-exif` | `false` | 是否使用EXIF数据。启用后使用照片EXIF中的相机参数和GPS信息。 |

## DEM选项（数字高程模型）

| 选项 | 默认值 | 说明 |
|------|--------|------|
| `dtm` | `false` | 是否生成数字地形模型（DTM）。DTM仅包含地面高程，不含建筑物和植被。 |
| `dsm` | `false` | 是否生成数字表面模型（DSM）。DSM包含所有地物（地面、建筑物、植被）的高程。 |
| `dem-gapfill-steps` | `3` | DEM空洞填充的迭代次数。用于填充DEM中的空白区域，值越大填充范围越广。 |
| `dem-resolution` | `5` | DEM分辨率（厘米/像素）。值越小，分辨率越高。 |
| `dem-decimation` | `1` | DEM降采样因子。每隔指定数量的像素采样一次，用于降低DEM分辨率。 |
| `dem-euclidean-map` | `false` | 是否生成欧几里得距离图。用于标识DEM中填充区域的距离信息。 |

## 瓦片选项

| 选项 | 默认值 | 说明 |
|------|--------|------|
| `tiles` | `false` | 是否生成栅格瓦片。将正射影像分割为Web地图标准瓦片（如XYZ瓦片）。 |
| `3d-tiles` | `false` | 是否生成3D Tiles格式。Cesium使用的三维数据格式，适用于Web三维可视化。 |

## 滚动快门选项

| 选项 | 默认值 | 说明 |
|------|--------|------|
| `rolling-shutter` | `false` | 是否处理滚动快门失真。适用于使用全局快门相机的无人机（如Phantom系列）。 |
| `rolling-shutter-readout` | `0` | 滚动快门读取时间（毫秒）。指定相机传感器的读取时间，用于校正失真。 |

## 概览和COG选项

| 选项 | 默认值 | 说明 |
|------|--------|------|
| `build-overviews` | `false` | 是否构建影像概览。在GeoTIFF中生成多分辨率概览，加快大文件显示速度。 |
| `cog` | `false` | 是否生成Cloud Optimized GeoTIFF（COG）。支持Web高效流式传输的GeoTIFF格式。 |

## 视频选项

| 选项 | 默认值 | 说明 |
|------|--------|------|
| `video-limit` | `500` | 从视频中提取的最大帧数。用于限制视频处理的工作量。 |
| `video-resolution` | `4000` | 视频提取帧的最大分辨率（像素宽度）。用于控制处理分辨率。 |

## 分块选项

| 选项 | 默认值 | 说明 |
|------|--------|------|
| `split` | `999999` | 将数据集分割为多个区块的阈值（图像数量）。超过此值时自动分块处理，适用于大型数据集。 |
| `split-overlap` | `150` | 分块之间的重叠距离（米）。相邻区块之间的重叠区域，用于保证拼接质量。 |
| `sm-no-align` | `false` | 是否禁用分块对齐优化。启用后不优化分块边界对齐。 |
| `sm-cluster` | `None` | 分块处理集群地址。用于分布式处理，格式为`host:port`。 |

## 合并选项

| 选项 | 默认值 | 说明 |
|------|--------|------|
| `merge` | `all` | 分块合并模式。可选值：`all`（合并所有结果）、`mosaic`（仅合并正射影像）、`pointcloud`（仅合并点云） |

## GPS选项

| 选项 | 默认值 | 说明 |
|------|--------|------|
| `force-gps` | `false` | 是否强制使用GPS信息。即使EXIF中有GPS也使用此选项进行强制约束。 |
| `gps-accuracy` | `3` | GPS精度（米）。用于SfM过程中的GPS权重计算，值越小权重越大。 |
| `gps-z-offset` | `0` | GPS高程偏移（米）。用于校正GPS高程数据与真实高程的差异。 |

## 磁盘优化选项

| 选项 | 默认值 | 说明 |
|------|--------|------|
| `optimize-disk-space` | `false` | 是否优化磁盘空间使用。启用后在处理过程中删除中间文件，减少磁盘占用。 |

## 波段选项

| 选项 | 默认值 | 说明 |
|------|--------|------|
| `primary-band` | `auto` | 主处理波段。指定用于处理的主要波段名称，`auto`表示自动选择。适用于多光谱影像。 |
| `skip-band-alignment` | `false` | 是否跳过波段对齐。启用后不校正多波段图像之间的偏移。 |

## GPU选项

| 选项 | 默认值 | 说明 |
|------|--------|------|
| `no-gpu` | `false` | 是否禁用GPU加速。启用后仅使用CPU进行计算，适用于无GPU或GPU驱动问题的环境。 |
