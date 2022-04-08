#!/bin/bash
DEV_NODE=ec2-50-17-68-106.compute-1.amazonaws.com
LOCAL_PATH=/Users/hchaoyan/source/FedML/
REMOTE_PATH=/fsx/hchaoyan/home/FedML
alias ws-sync='rsync -avP -e ssh --exclude '.idea' $LOCAL_PATH $DEV_NODE:$REMOTE_PATH'
ws-sync; fswatch -o $LOCAL_PATH | while read f; do ws-sync; done