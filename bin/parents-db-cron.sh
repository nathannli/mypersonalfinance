#!/bin/bash

# Function to send Discord notification
send_discord_notification() {
    local message="$1"
    curl -X POST http://localhost:30008/alert \
        -H "Content-Type: application/json" \
        -d "{\"message\": \"$message\"}" \
        --silent --show-error
}

# Configurable variables
NAS_IP=195.168.1.14
NAS_USER=$PARENTS_FINANCE_FTP_USER
NAS_PASSWORD=$PARENTS_FINANCE_FTP_PASS
GIT_PATH="/home/nathan/git/mypersonalfinance"

echo "NAS_IP: $NAS_IP"
echo "NAS_USER: $NAS_USER"
echo "NAS_PASSWORD: $NAS_PASSWORD"


files_to_process=(
    "ftp://${NAS_USER}:${NAS_PASSWORD}@${NAS_IP}/1.2_FamilyExpenseSHARED/CdnTireMC5989_2025.xlsx"
    "ftp://${NAS_USER}:${NAS_PASSWORD}@${NAS_IP}/1.2_FamilyExpenseSHARED/CostCoMC1969_2025.xlsx"
    "ftp://${NAS_USER}:${NAS_PASSWORD}@${NAS_IP}/1.2_FamilyExpenseSHARED/ROGERMC4804_2025.xlsx"
    "ftp://${NAS_USER}:${NAS_PASSWORD}@${NAS_IP}/1.2_FamilyExpenseSHARED/TDCheq0307_YTD2025.xlsx"
    "ftp://${NAS_USER}:${NAS_PASSWORD}@${NAS_IP}/1.2_FamilyExpenseSHARED/TDVisa6413_YTD2025.xlsx"
)

failed_files=()

for file in "${files_to_process[@]}"; do
    echo "Processing $file"
    if ! uv run python "$GIT_PATH/load-excel-transactions.py" --filepath "$file" --cron true; then
        echo "ERROR: Failed to process $file"
        failed_files+=("$file")
    fi
done

# Send Discord notification if any files failed
if [ ${#failed_files[@]} -gt 0 ]; then
    failed_list=$(printf '%s\n' "${failed_files[@]}")
    error_message="⚠️ Parents Finance Cron Job: ${#failed_files[@]} file(s) failed to process:\n\`\`\`\n$failed_list\n\`\`\`"
    send_discord_notification "$error_message"
fi
