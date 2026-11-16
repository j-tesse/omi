# Imported automatically by Python at startup because this directory is on PYTHONPATH
# (docker-compose.yml, backend/pusher/llm-gateway). Installs the import hook that
# applies omi_local_patches to upstream modules as they load. Upstream code is untouched.
import omi_local_patches

omi_local_patches.install()
