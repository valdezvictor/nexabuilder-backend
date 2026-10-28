#!/bin/bash
# deploy-cms.sh — Deploy Step 4 CMS to EC2 + run Alembic migration
# Run from: /Users/victorvaldez/workspace/nexabuilder/nexabuilder-backend
# Usage: bash deploy-cms.sh

EC2="ec2-user@api.nexabuilder.com"
REMOTE="/home/ec2-user/nexabuilder-backend"
LOCAL_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "=== NexaBuilder Step 4: CMS Deploy ==="
echo ""

echo "1. Copying files to EC2..."
scp -o StrictHostKeyChecking=no \
  "$LOCAL_DIR/app/models/content_block.py" \
  "$EC2:$REMOTE/app/models/content_block.py"

scp -o StrictHostKeyChecking=no \
  "$LOCAL_DIR/app/schemas/content_block.py" \
  "$EC2:$REMOTE/app/schemas/content_block.py"

scp -o StrictHostKeyChecking=no \
  "$LOCAL_DIR/app/routers/api/content.py" \
  "$EC2:$REMOTE/app/routers/api/content.py"

scp -o StrictHostKeyChecking=no \
  "$LOCAL_DIR/app/migrations/versions/a1b2c3d4e5f6_add_content_blocks_cms_table.py" \
  "$EC2:$REMOTE/app/migrations/versions/a1b2c3d4e5f6_add_content_blocks_cms_table.py"

scp -o StrictHostKeyChecking=no \
  "$LOCAL_DIR/app/migrations/env.py" \
  "$EC2:$REMOTE/app/migrations/env.py"

scp -o StrictHostKeyChecking=no \
  "$LOCAL_DIR/app/main.py" \
  "$EC2:$REMOTE/app/main.py"

scp -o StrictHostKeyChecking=no \
  "$LOCAL_DIR/app/models/otp_code.py" \
  "$EC2:$REMOTE/app/models/otp_code.py"

scp -o StrictHostKeyChecking=no \
  "$LOCAL_DIR/app/models/contractor_account.py" \
  "$EC2:$REMOTE/app/models/contractor_account.py"

scp -o StrictHostKeyChecking=no \
  "$LOCAL_DIR/app/services/otp_service.py" \
  "$EC2:$REMOTE/app/services/otp_service.py"

scp -o StrictHostKeyChecking=no \
  "$LOCAL_DIR/app/routers/api/verify.py" \
  "$EC2:$REMOTE/app/routers/api/verify.py"

scp -o StrictHostKeyChecking=no \
  "$LOCAL_DIR/app/routers/api/lead_intake.py" \
  "$EC2:$REMOTE/app/routers/api/lead_intake.py"

scp -o StrictHostKeyChecking=no \
  "$LOCAL_DIR/app/migrations/versions/b2c3d4e5f6a7_add_verification_gate.py" \
  "$EC2:$REMOTE/app/migrations/versions/b2c3d4e5f6a7_add_verification_gate.py"

scp -o StrictHostKeyChecking=no \
  "$LOCAL_DIR/app/models/property_assessment.py" \
  "$EC2:$REMOTE/app/models/property_assessment.py"

scp -o StrictHostKeyChecking=no \
  "$LOCAL_DIR/app/models/active_project.py" \
  "$EC2:$REMOTE/app/models/active_project.py"

scp -o StrictHostKeyChecking=no \
  "$LOCAL_DIR/app/services/address_service.py" \
  "$EC2:$REMOTE/app/services/address_service.py"

scp -o StrictHostKeyChecking=no \
  "$LOCAL_DIR/app/services/assessment_gate.py" \
  "$EC2:$REMOTE/app/services/assessment_gate.py"

scp -o StrictHostKeyChecking=no \
  "$LOCAL_DIR/app/migrations/versions/c3d4e5f6a7b8_add_property_assessments_active_projects.py" \
  "$EC2:$REMOTE/app/migrations/versions/c3d4e5f6a7b8_add_property_assessments_active_projects.py"

scp -o StrictHostKeyChecking=no \
  "$LOCAL_DIR/app/routers/api/partner_routing.py" \
  "$EC2:$REMOTE/app/routers/api/partner_routing.py"
scp -o StrictHostKeyChecking=no \
  "$LOCAL_DIR/app/models/assessment_rate_log.py" \
  "$EC2:$REMOTE/app/models/assessment_rate_log.py"

scp -o StrictHostKeyChecking=no \
  "$LOCAL_DIR/app/migrations/versions/d4e5f6a7b8c9_add_assessment_rate_limit.py" \
  "$EC2:$REMOTE/app/migrations/versions/d4e5f6a7b8c9_add_assessment_rate_limit.py"

echo ""
echo "2. Running Alembic migration + restarting API on EC2..."
ssh -o StrictHostKeyChecking=no "$EC2" << 'REMOTE_CMDS'
cd /home/ec2-user/nexabuilder-backend
source venv/bin/activate

# Fetch CMS_ADMIN_KEY from SSM and add to .env
CMS_KEY=$(aws ssm get-parameter --name /nexabuilder/cms/ADMIN_KEY --with-decryption --query Parameter.Value --output text --region us-west-1 2>/dev/null)
if [ -n "$CMS_KEY" ]; then
  if grep -q "CMS_ADMIN_KEY" .env 2>/dev/null; then
    sed -i "s/^CMS_ADMIN_KEY=.*/CMS_ADMIN_KEY=$CMS_KEY/" .env
  else
    echo "CMS_ADMIN_KEY=$CMS_KEY" >> .env
  fi
  echo "  ✓ CMS_ADMIN_KEY written to .env"
fi

echo ""
echo "Running migration..."
alembic upgrade head
MIGRATION_STATUS=$?

if [ $MIGRATION_STATUS -eq 0 ]; then
  echo "  ✓ Migration complete"
else
  echo "  ✗ Migration failed — check output above"
  exit 1
fi

echo ""
echo "Restarting API service..."
sudo systemctl restart nexabuilder-api
sleep 3
systemctl status nexabuilder-api --no-pager | head -8
echo ""
echo "\nVerifying new endpoints..."
curl -s -o /dev/null -w "  /api/verify/request → HTTP %{http_code}\n" -X POST https://api.nexabuilder.com/api/verify/request -H "Content-Type: application/json" -d '{"user_id":"test","channel":"email"}'
echo "=== Deploy complete ==="
REMOTE_CMDS
