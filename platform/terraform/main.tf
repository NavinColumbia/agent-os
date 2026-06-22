# platform/terraform/main.tf — cloud target for agent-os (AWS reference; GCP/Azure analogous).
#
# This is the SKELETON that maps platform/inventory.yaml -> managed services. It is intentionally
# minimal and SAFE: nothing here is applied for you. Review, set variables, then:
#     terraform init && terraform plan        # see what WOULD be created
#     terraform apply                         # only when you decide to scale to cloud
#
# The migration principle (ADR / CLOUD-MIGRATION.md): same code, bigger endpoints. After apply,
# point .env.local at the outputs (DATABASE_URL, OBJSTORE_BACKEND=s3 + bucket, secrets ARN) and the
# exact same agent-os runs — DBOS/audit/vault/objstore just follow the URLs.

terraform {
  required_version = ">= 1.5"
  required_providers {
    aws = { source = "hashicorp/aws", version = "~> 5.0" }
  }
  # Recommended: remote state so a buyer/teammate shares one source of truth.
  # backend "s3" { bucket = "agent-os-tfstate" key = "prod/terraform.tfstate" region = "us-east-1" }
}

provider "aws" {
  region = var.region
}

# ── Postgres (state of everything) → RDS with pgvector ─────────────────────────
resource "aws_db_instance" "agentos" {
  identifier            = "${var.name}-postgres"
  engine                = "postgres"
  engine_version        = var.postgres_version
  instance_class        = var.db_instance_class
  allocated_storage     = var.db_storage_gb
  max_allocated_storage = var.db_storage_gb * 4 # autoscale storage
  db_name               = "agentos"
  username              = "agentos"
  password              = var.db_password # pass via TF_VAR_db_password / secrets, never commit
  storage_encrypted     = true
  multi_az              = var.multi_az
  backup_retention_period = 7
  deletion_protection   = true
  skip_final_snapshot   = false
  final_snapshot_identifier = "${var.name}-postgres-final"
  vpc_security_group_ids = [aws_security_group.db.id]
  tags = local.tags
  # pgvector: enable the extension after first boot (CREATE EXTENSION vector;) via your migration.
}

# ── Object store (media/blobs) → S3 ───────────────────────────────────────────
resource "aws_s3_bucket" "objstore" {
  bucket = "${var.name}-objstore"
  tags   = local.tags
}
resource "aws_s3_bucket_versioning" "objstore" {
  bucket = aws_s3_bucket.objstore.id
  versioning_configuration { status = "Enabled" }
}
resource "aws_s3_bucket_server_side_encryption_configuration" "objstore" {
  bucket = aws_s3_bucket.objstore.id
  rule { apply_server_side_encryption_by_default { sse_algorithm = "AES256" } }
}
resource "aws_s3_bucket_public_access_block" "objstore" {
  bucket                  = aws_s3_bucket.objstore.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# ── Secrets vault → AWS Secrets Manager ────────────────────────────────────────
resource "aws_secretsmanager_secret" "agentos" {
  name = "${var.name}/runtime"
  tags = local.tags
}

# ── Network: DB reachable only from the app/compute SG (never public) ──────────
resource "aws_security_group" "db" {
  name_prefix = "${var.name}-db-"
  description = "agent-os Postgres — private only"
  vpc_id      = var.vpc_id
  ingress {
    description     = "Postgres from app compute only"
    from_port       = 5432
    to_port         = 5432
    protocol        = "tcp"
    security_groups = var.app_security_group_ids
  }
  egress { from_port = 0, to_port = 0, protocol = "-1", cidr_blocks = ["0.0.0.0/0"] }
  tags = local.tags
}

locals {
  tags = { app = "agent-os", managed_by = "terraform", owner = var.owner }
}

# NATS (Synadia Cloud) and Cerbos Cloud are SaaS — configure via their providers/URLs, not here.
# Controller/api/bridge → containers on ECS/EKS (add an aws_ecs_service module when you containerize).
# GPU pool (image/music-gen, training) → a separate GPU node group / Runpod, provisioned on demand.
