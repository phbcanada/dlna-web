#!/usr/bin/bash

sudo cp dlna-web-local.service /etc/systemd/system/dlna-web.service
sudo systemctl daemon-reload
# sudo systemctl enable --now dlna-web
sudo systemctl restart dlna-web
sudo systemctl status dlna-web
