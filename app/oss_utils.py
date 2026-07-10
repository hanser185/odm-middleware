import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class OssConfig:
    access_key_id: str
    access_key_secret: str
    endpoint: str
    bucket: str
    prefix: str = ""
    public_base_url: str | None = None


def _clean_endpoint(endpoint: str):
    return endpoint.removeprefix("https://").removeprefix("http://").rstrip("/")


def _clean_prefix(prefix: str | None):
    if not prefix:
        return ""
    return prefix.strip("/")


def get_oss_config():
    access_key_id = os.getenv("ALIYUN_OSS_ACCESS_KEY_ID")
    access_key_secret = os.getenv("ALIYUN_OSS_ACCESS_KEY_SECRET")
    endpoint = os.getenv("ALIYUN_OSS_ENDPOINT")
    bucket = os.getenv("ALIYUN_OSS_BUCKET")
    if not all([access_key_id, access_key_secret, endpoint, bucket]):
        return None

    return OssConfig(
        access_key_id=access_key_id,
        access_key_secret=access_key_secret,
        endpoint=_clean_endpoint(endpoint),
        bucket=bucket,
        prefix=_clean_prefix(os.getenv("ALIYUN_OSS_PREFIX")),
        public_base_url=(os.getenv("ALIYUN_OSS_PUBLIC_BASE_URL") or "").rstrip("/") or None,
    )


def build_oss_key(config: OssConfig, object_name: str):
    object_name = object_name.lstrip("/")
    if config.prefix:
        return f"{config.prefix}/{object_name}"
    return object_name


def build_oss_url(config: OssConfig, key: str):
    if config.public_base_url:
        return f"{config.public_base_url}/{key}"
    return f"https://{config.bucket}.{config.endpoint}/{key}"


def upload_file_to_oss(file_path: str, object_name: str):
    config = get_oss_config()
    if config is None:
        return None

    try:
        import oss2
    except ImportError as exc:
        raise RuntimeError("oss2 is required when Aliyun OSS environment variables are configured") from exc

    path = Path(file_path)
    if not path.exists():
        raise RuntimeError(f"OSS upload file not found: {file_path}")

    key = build_oss_key(config, object_name)
    auth = oss2.Auth(config.access_key_id, config.access_key_secret)
    bucket = oss2.Bucket(auth, f"https://{config.endpoint}", config.bucket)
    bucket.put_object_from_file(key, str(path))
    return {
        "bucket": config.bucket,
        "key": key,
        "url": build_oss_url(config, key),
    }
