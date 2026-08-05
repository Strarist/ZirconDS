"""One-shot: public-read policy for properties/ + data/, upload archive JSON."""

from __future__ import annotations

import json
import os
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load_dotenv(path: Path) -> None:
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip("'").strip('"')
        if key and key not in os.environ:
            os.environ[key] = value


def main() -> None:
    load_dotenv(ROOT / ".env")
    import boto3

    bucket = os.environ["AWS_S3_BUCKET"]
    region = os.environ["AWS_REGION"]
    s3 = boto3.client(
        "s3",
        region_name=region,
        aws_access_key_id=os.environ["AWS_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["AWS_SECRET_ACCESS_KEY"],
    )

    policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "PublicReadPropertiesAndData",
                "Effect": "Allow",
                "Principal": "*",
                "Action": ["s3:GetObject"],
                "Resource": [
                    f"arn:aws:s3:::{bucket}/properties/*",
                    f"arn:aws:s3:::{bucket}/data/*",
                ],
            }
        ],
    }
    s3.put_bucket_policy(Bucket=bucket, Policy=json.dumps(policy))
    print("policy_applied")

    props = ROOT / "output" / "properties.json"
    s3.upload_file(
        str(props),
        bucket,
        "data/properties.json",
        ExtraArgs={"ContentType": "application/json"},
    )
    print("uploaded data/properties.json")

    latest = ROOT / "output" / "latest.json"
    if latest.is_file():
        s3.upload_file(
            str(latest),
            bucket,
            "data/latest.json",
            ExtraArgs={"ContentType": "application/json"},
        )
        print("uploaded data/latest.json")

    sample = (
        f"https://{bucket}.s3.{region}.amazonaws.com/"
        "properties/anantraj-the-estate-residences-gurgaon-sector-63a-4-4781/"
        "c093c8ebcdc6737d.webp"
    )
    data_url = f"https://{bucket}.s3.{region}.amazonaws.com/data/properties.json"
    for label, url in (("image", sample), ("json", data_url)):
        try:
            with urllib.request.urlopen(url, timeout=30) as resp:
                print(f"{label}_public", resp.status, resp.headers.get("Content-Length"))
        except Exception as exc:  # noqa: BLE001
            print(f"{label}_public_fail", exc)


if __name__ == "__main__":
    main()
