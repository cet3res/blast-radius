IMAGE ?= blast-radius-entry
PYTHON ?= python3
SAMPLE := $(CURDIR)/sample
OUTPUT := $(CURDIR)/output.jsonl

# The flags the scorer runs your image with. The scorer is linux/amd64, so on
# an Apple Silicon Mac --platform makes your local run match it.
RUN_FLAGS := --rm --platform linux/amd64 --network none --cpus 2 \
	--memory 4096m --memory-swap 4096m --pids-limit 256 --read-only \
	--tmpfs /tmp:rw,size=64m,mode=1777 --cap-drop ALL \
	--security-opt no-new-privileges --user 65534:65534 --env HOME=/tmp \
	--volume $(SAMPLE)/commits:/data/commits:ro \
	--volume $(SAMPLE)/tests.json:/data/tests.json:ro

.PHONY: build sample check

build:
	docker build --platform linux/amd64 --tag $(IMAGE) .

# Runs your image once on the sample commits under the scorer's limits and
# scores what it printed.
sample: build
	docker run $(RUN_FLAGS) $(IMAGE) > $(OUTPUT)
	python3 score.py $(SAMPLE) $(SAMPLE)/answers.json $(OUTPUT)

# The output's rules only: every line well formed and every commit answered.
# A wrong selection still passes here; make sample counts the misses.
check: build
	docker run $(RUN_FLAGS) $(IMAGE) > $(OUTPUT)
	python3 score.py $(SAMPLE) $(SAMPLE)/answers.json $(OUTPUT) > /dev/null && echo "format ok"

.PHONY: sample-local test map
sample-local:
	$(PYTHON) selector.py --data $(SAMPLE) > $(OUTPUT)
	$(PYTHON) score.py $(SAMPLE) $(SAMPLE)/answers.json $(OUTPUT)

test:
	$(PYTHON) -m unittest discover -s tests -v

# Use Python 3.13 with requirements.lock and coverage==7.10.7 installed.
# SOURCE must be an unmodified checkout at the competition's pinned commit.
map:
	$(PYTHON) tools/build_map.py "$(SOURCE)" sample/tests.json dependencies.json.gz
	$(PYTHON) tools/build_profile.py "$(SOURCE)" dependencies.json.gz dependencies.json.gz --python $(PYTHON)
	$(PYTHON) tools/build_proxies.py "$(SOURCE)" dependencies.json.gz dependencies.json.gz
