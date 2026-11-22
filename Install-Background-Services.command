#!/bin/zsh
set -eu
cd "${0:A:h}"
print 'Spliff background services'
print 'Stop the old Spliff tunnel with Control+C in its Terminal window before continuing.'
print 'The tunnel runtime key is requested once, hidden, and saved locally. Do not paste it into chat.'
print
if ! .venv/bin/python -m spliff --db "$PWD/artist-workflow.sqlite" services install; then
  print '\nInstallation did not complete. Read the message above.'
  read -r '?Press Return to close.'
  exit 1
fi
print '\nServices installed. Checking startup…'
.venv/bin/python -m spliff services status
print '\nDashboard: http://127.0.0.1:8765'
print 'Refresh the Spliff connection tools in ChatGPT after the tunnel reports ready.'
read -r '?Press Return to close. The services keep running.'
