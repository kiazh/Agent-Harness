# Host-side scripts mounted read-only into the Compose deployment at /app/scripts
# (MACHINA_SCRIPTS_DIR). Drop operator scripts here; they are ignored by git
# except for this README. The `docker run`/compose mount expects this directory
# to exist on the host (INFRA-m1: previously the mount target did not exist and
# dockerd auto-created it root-owned on Linux).
