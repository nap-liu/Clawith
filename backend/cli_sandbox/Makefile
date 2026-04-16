# Build / tag / publish the clawith-cli-sandbox image.
#
# REGISTRY and IMAGE must be provided by the caller (from a .env file or the
# shell environment). We deliberately do not hard-code a registry hostname.

REGISTRY ?= $(error REGISTRY is not set — export REGISTRY=your-registry-host)
IMAGE    ?= clawith-cli-sandbox
BASE     ?= debian-bookworm-slim
DATE     := $(shell date -u +%Y%m%d)
TAG      := $(BASE)-$(DATE)

.PHONY: build tag push promote-stable all

build:
	docker build -t $(IMAGE):$(TAG) .

tag: build
	docker tag $(IMAGE):$(TAG) $(REGISTRY)/$(IMAGE):$(TAG)

push: tag
	docker push $(REGISTRY)/$(IMAGE):$(TAG)

# Operator: only run once a build has been validated in staging.
promote-stable:
	docker tag $(REGISTRY)/$(IMAGE):$(TAG) $(REGISTRY)/$(IMAGE):stable
	docker push $(REGISTRY)/$(IMAGE):stable

all: push
