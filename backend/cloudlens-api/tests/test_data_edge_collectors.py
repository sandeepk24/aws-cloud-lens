"""Tests for RDS, S3, WAFv2, Route 53, and CloudWatch Logs collectors."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock

from app.accounts.models import ProfileAuth
from app.collectors.logs import CloudWatchLogGroupCollector, create_logs_collectors
from app.collectors.rds import RdsClusterCollector, RdsInstanceCollector, create_rds_collectors
from app.collectors.route53 import Route53HostedZoneCollector, create_route53_collectors
from app.collectors.s3 import S3BucketCollector, create_s3_collectors, resolve_bucket_region
from app.collectors.wafv2 import (
    Wafv2CloudFrontWebAclCollector,
    Wafv2RegionalWebAclCollector,
    create_wafv2_collectors,
)
from app.connectors.aws.client import AWSConnector
from app.connectors.aws.session import AWSSessionManager

ACCOUNT = "111122223333"
REGION = "test-region-a"
GLOBAL = "test-region-global"


def _mgr(regions: tuple[str, ...] = (REGION, GLOBAL)) -> AWSSessionManager:
    return AWSSessionManager(
        AWSConnector(
            project_alias="demo",
            account_alias="acct",
            account_id=ACCOUNT,
            regions=regions,
            enabled_collectors=("s3.bucket",),
            auth=ProfileAuth(type="profile", profile="p"),
        ),
        sts_service_name="sts",
        session_factory=MagicMock(),
    )


def test_resolve_bucket_region_aliases() -> None:
    assert (
        resolve_bucket_region(None, default_region="test-region-a", alias_map={})
        == "test-region-a"
    )
    assert (
        resolve_bucket_region("", default_region="test-region-a", alias_map={})
        == "test-region-a"
    )
    assert (
        resolve_bucket_region(
            "EU", default_region="test-region-a", alias_map={"EU": "test-region-eu"}
        )
        == "test-region-eu"
    )
    assert (
        resolve_bucket_region("test-region-b", default_region="test-region-a", alias_map={})
        == "test-region-b"
    )


def test_rds_instance_and_cluster_metadata_only() -> None:
    client = MagicMock()
    client.describe_db_instances.return_value = {
        "DBInstances": [
            {
                "DBInstanceIdentifier": "db-1",
                "DBInstanceArn": f"arn:aws:rds:{REGION}:{ACCOUNT}:db:db-1",
                "DBInstanceStatus": "available",
                "Engine": "aurora-postgresql",
                "EngineVersion": "15.4",
                "DBInstanceClass": "db.r6g.large",
                "MultiAZ": False,
                "StorageEncrypted": True,
                "PubliclyAccessible": False,
                "DBClusterIdentifier": "aurora-1",
                "Endpoint": {"Address": "db-1.example", "Port": 5432},
                "DBSubnetGroup": {
                    "VpcId": "vpc-1",
                    "Subnets": [{"SubnetIdentifier": "subnet-1"}],
                },
                "VpcSecurityGroups": [{"VpcSecurityGroupId": "sg-1"}],
                "TagList": [{"Key": "Name", "Value": "db-1"}],
                "MasterUsername": "admin",
            }
        ]
    }
    inst = RdsInstanceCollector(
        _mgr(),
        rds_service_name="rds",
        ec2_service_name="ec2",
        client_factory=lambda _r: client,
        sleep=lambda _s: None,
    ).fetch_page(account_id=ACCOUNT, region=REGION).items[0]
    assert inst.observed_state["status"] == "available"
    assert inst.observed_state["engine"] == "aurora-postgresql"
    assert "MasterUserPassword" not in inst.observed_state
    assert "password" not in str(inst.observed_state).lower()

    client.describe_db_clusters.return_value = {
        "DBClusters": [
            {
                "DBClusterIdentifier": "aurora-1",
                "DBClusterArn": f"arn:aws:rds:{REGION}:{ACCOUNT}:cluster:aurora-1",
                "Status": "available",
                "Engine": "aurora-postgresql",
                "Endpoint": "cluster.example",
                "ReaderEndpoint": "ro.example",
                "DBClusterMembers": [{"DBInstanceIdentifier": "db-1"}],
                "StorageEncrypted": True,
                "TagList": [],
            }
        ]
    }
    cluster = RdsClusterCollector(
        _mgr(),
        rds_service_name="rds",
        ec2_service_name="ec2",
        client_factory=lambda _r: client,
        sleep=lambda _s: None,
    ).fetch_page(account_id=ACCOUNT, region=REGION).items[0]
    assert cluster.observed_state["db_cluster_members"] == ["db-1"]


def test_s3_filters_by_bucket_region_and_skips_object_apis() -> None:
    client = MagicMock()
    client.list_buckets.return_value = {
        "Buckets": [
            {"Name": "local-bucket", "CreationDate": datetime(2024, 1, 1, tzinfo=UTC)},
            {"Name": "other-bucket", "CreationDate": datetime(2024, 1, 2, tzinfo=UTC)},
        ]
    }

    def get_bucket_location(**kwargs):
        if kwargs["Bucket"] == "local-bucket":
            return {"LocationConstraint": None}
        return {"LocationConstraint": "test-region-b"}

    client.get_bucket_location.side_effect = get_bucket_location
    client.get_bucket_versioning.return_value = {"Status": "Enabled"}
    client.get_bucket_encryption.return_value = {
        "ServerSideEncryptionConfiguration": {
            "Rules": [
                {
                    "ApplyServerSideEncryptionByDefault": {
                        "SSEAlgorithm": "aws:kms",
                        "KMSMasterKeyID": "key-id-only",
                    },
                    "BucketKeyEnabled": True,
                }
            ]
        }
    }
    client.get_public_access_block.return_value = {
        "PublicAccessBlockConfiguration": {
            "BlockPublicAcls": True,
            "IgnorePublicAcls": True,
            "BlockPublicPolicy": True,
            "RestrictPublicBuckets": True,
        }
    }
    client.get_bucket_ownership_controls.return_value = {
        "OwnershipControls": {"Rules": [{"ObjectOwnership": "BucketOwnerEnforced"}]}
    }

    items = S3BucketCollector(
        _mgr(),
        s3_service_name="s3",
        s3_default_region=REGION,
        s3_location_constraint_map={},
        client_factory=lambda _r: client,
        sleep=lambda _s: None,
    ).fetch_page(account_id=ACCOUNT, region=REGION).items

    assert [i.resource_id for i in items] == ["local-bucket"]
    assert items[0].region == REGION
    assert items[0].observed_state["versioning_status"] == "Enabled"
    assert items[0].observed_state["data_plane_access"] is False
    assert not client.list_objects.called
    assert not client.list_objects_v2.called
    assert not client.get_object.called
    assert not hasattr(client, "select_object_content") or not client.select_object_content.called


def test_wafv2_regional_and_cloudfront_scope_routing() -> None:
    client = MagicMock()
    client.list_web_acls.return_value = {
        "WebACLs": [
            {
                "Name": "acl-1",
                "Id": "id-1",
                "ARN": f"arn:aws:wafv2:{REGION}:{ACCOUNT}:regional/webacl/acl-1/id-1",
            }
        ]
    }
    client.get_web_acl.return_value = {
        "WebACL": {
            "Name": "acl-1",
            "Id": "id-1",
            "ARN": f"arn:aws:wafv2:{REGION}:{ACCOUNT}:regional/webacl/acl-1/id-1",
            "Description": "web",
            "Capacity": 50,
            "DefaultAction": {"Allow": {}},
            "Rules": [
                {
                    "Name": "rule-1",
                    "Priority": 1,
                    "Action": {"Block": {}},
                    "Statement": {"ByteMatchStatement": {}},
                }
            ],
        }
    }
    client.list_resources_for_web_acl.return_value = {
        "ResourceArns": [f"arn:aws:elasticloadbalancing:{REGION}:{ACCOUNT}:loadbalancer/app/x"]
    }

    regional = Wafv2RegionalWebAclCollector(
        _mgr(),
        wafv2_service_name="wafv2",
        global_control_region=GLOBAL,
        client_factory=lambda _r: client,
        sleep=lambda _s: None,
    ).fetch_page(account_id=ACCOUNT, region=REGION).items[0]
    assert regional.observed_state["scope"] == "REGIONAL"
    assert regional.observed_state["association_count"] == 1
    assert client.list_web_acls.call_args.kwargs["Scope"] == "REGIONAL"

    # CloudFront collector skips non-global regions.
    cf = Wafv2CloudFrontWebAclCollector(
        _mgr(),
        wafv2_service_name="wafv2",
        global_control_region=GLOBAL,
        client_factory=lambda _r: client,
        sleep=lambda _s: None,
    )
    skipped = cf.fetch_page(account_id=ACCOUNT, region=REGION)
    assert skipped.items == []

    client.list_web_acls.return_value = {
        "WebACLs": [
            {
                "Name": "cf-acl",
                "Id": "cf-id",
                "ARN": f"arn:aws:wafv2:{GLOBAL}:{ACCOUNT}:global/webacl/cf-acl/cf-id",
            }
        ]
    }
    client.get_web_acl.return_value = {
        "WebACL": {
            "Name": "cf-acl",
            "Id": "cf-id",
            "DefaultAction": {"Allow": {}},
            "Rules": [],
        }
    }
    client.list_resources_for_web_acl.return_value = {
        "ResourceArns": ["arn:aws:cloudfront::111122223333:distribution/E123"]
    }
    cf_items = cf.fetch_page(account_id=ACCOUNT, region=GLOBAL).items
    assert len(cf_items) == 1
    assert cf_items[0].observed_state["scope"] == "CLOUDFRONT"
    assert client.list_web_acls.call_args.kwargs["Scope"] == "CLOUDFRONT"


def test_route53_hosted_zone_record_count_without_record_values() -> None:
    client = MagicMock()
    client.list_hosted_zones.return_value = {
        "HostedZones": [
            {
                "Id": "/hostedzone/Z1",
                "Name": "example.com.",
                "CallerReference": "ref",
                "Config": {"PrivateZone": False},
                "ResourceRecordSetCount": 12,
            }
        ],
        "IsTruncated": False,
    }
    client.get_hosted_zone.return_value = {
        "HostedZone": {
            "Id": "/hostedzone/Z1",
            "Name": "example.com.",
            "Config": {"PrivateZone": False, "Comment": "prod"},
            "ResourceRecordSetCount": 12,
        }
    }
    collector = Route53HostedZoneCollector(
        _mgr(),
        route53_service_name="route53",
        global_control_region=GLOBAL,
        client_factory=lambda _r: client,
        sleep=lambda _s: None,
    )
    assert collector.fetch_page(account_id=ACCOUNT, region=REGION).items == []
    item = collector.fetch_page(account_id=ACCOUNT, region=GLOBAL).items[0]
    assert item.observed_state["resource_record_set_count"] == 12
    assert item.observed_state["record_values_collected"] is False
    assert not client.list_resource_record_sets.called


def test_cloudwatch_log_group_metadata_only() -> None:
    client = MagicMock()
    client.describe_log_groups.return_value = {
        "logGroups": [
            {
                "logGroupName": "/app/api",
                "arn": f"arn:aws:logs:{REGION}:{ACCOUNT}:log-group:/app/api:*",
                "retentionInDays": 14,
                "storedBytes": 1024,
                "metricFilterCount": 2,
                "kmsKeyId": "arn:aws:kms:key/abc",
                "creationTime": 1_700_000_000_000,
            }
        ]
    }
    item = CloudWatchLogGroupCollector(
        _mgr(),
        logs_service_name="logs",
        client_factory=lambda _r: client,
        sleep=lambda _s: None,
    ).fetch_page(account_id=ACCOUNT, region=REGION).items[0]
    assert item.observed_state["retention_in_days"] == 14
    assert item.observed_state["log_events_collected"] is False
    assert not client.get_log_events.called
    assert not client.filter_log_events.called
    assert not client.start_query.called


def test_factory_helpers() -> None:
    mgr = _mgr()
    assert [c.name for c in create_rds_collectors(mgr, rds_service_name="rds", ec2_service_name="ec2")] == [
        "rds.db_instance",
        "rds.db_cluster",
    ]
    assert [c.name for c in create_s3_collectors(
        mgr, s3_service_name="s3", s3_default_region=REGION
    )] == ["s3.bucket"]
    assert [c.name for c in create_wafv2_collectors(
        mgr, wafv2_service_name="wafv2", global_control_region=GLOBAL
    )] == ["wafv2.web_acl_regional", "wafv2.web_acl_cloudfront"]
    assert [c.name for c in create_route53_collectors(
        mgr, route53_service_name="route53", global_control_region=GLOBAL
    )] == ["route53.hosted_zone"]
    assert [c.name for c in create_logs_collectors(mgr, logs_service_name="logs")] == [
        "logs.log_group"
    ]
