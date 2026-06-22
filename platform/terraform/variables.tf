# platform/terraform/variables.tf — set these via terraform.tfvars or TF_VAR_* env vars.
# Secrets (db_password) should come from the environment or a secrets store, NEVER a committed file.

variable "name"    { type = string  default = "agent-os" }
variable "owner"   { type = string  default = "navinashok-swaminathan" }
variable "region"  { type = string  default = "us-east-1" }

variable "postgres_version"  { type = string default = "16.4" }
variable "db_instance_class" { type = string default = "db.t4g.medium" } # scale up as load grows
variable "db_storage_gb"     { type = number default = 50 }
variable "multi_az"          { type = bool   default = false }           # true for HA in prod
variable "db_password"       { type = string sensitive = true }          # TF_VAR_db_password=...

variable "vpc_id"                 { type = string }                      # your VPC
variable "app_security_group_ids" { type = list(string) default = [] }   # compute that may reach the DB

output "database_url_hint" {
  value = "postgresql://agentos:<db_password>@${aws_db_instance.agentos.address}:5432/agentos"
}
output "objstore_bucket" { value = aws_s3_bucket.objstore.bucket }
output "secrets_arn"     { value = aws_secretsmanager_secret.agentos.arn }
