# Zolo website quality gate. Needs python3 and Chrome/Chromium (CHROME=/path to override).
.PHONY: check check-static serve

check:            ## static + real-browser checks (the pre-deploy gate)
	python3 tests/check_site.py

check-static:     ## static checks only (no browser needed)
	python3 tests/check_site.py --static

serve:            ## preview locally at http://127.0.0.1:8000/
	python3 -m http.server 8000 --bind 127.0.0.1
