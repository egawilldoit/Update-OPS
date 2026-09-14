#!/usr/bin/env bash
# Source ONLY via BASH_ENV when running upgrade.sh in a designated disposable VM.
# Pause before the selected operation, preserving the deployment process and lock.
unset BASH_ENV
python3 "${EGA_VM_CHECKOUT:?}/deploy/tests/vm-contract-acceptance.py" guard   --report "${EGA_VM_BEFORE_REPORT:?}.guard" || exit 2
set -T
vm_checkpoint() {
  local command_text="$1" matched=0
  [ "${EGA_VM_CHECKPOINT_TAKEN:-0}" = 0 ] || return 0
  case "${EGA_VM_CHECKPOINT:?}" in
    reboot_after_drain) [[ "$command_text" == *'waiting for quiescence via deploy controller'* ]] && matched=1 ;;
    reboot_after_migration) [[ "$command_text" == 'ega_maybe_fail pre-switch '* ]] && matched=1 ;;
    reboot_after_switch) [[ "$command_text" == 'ega_maybe_fail post-switch '* ]] && matched=1 ;;
    *) return 2 ;;
  esac
  [ "$matched" = 1 ] || return 0
  export EGA_VM_CHECKPOINT_TAKEN=1
  trap - DEBUG
  python3 "${EGA_VM_CHECKOUT:?}/deploy/tests/vm-contract-acceptance.py" snapshot \
    --case "$EGA_VM_CHECKPOINT" --report "${EGA_VM_BEFORE_REPORT:?}"
  # snapshot intentionally returns nonzero: evidence is not a passed acceptance.
  python3 -c 'import json,sys; assert json.load(open(sys.argv[1]))["status"] == "BEFORE_REBOOT"' "$EGA_VM_BEFORE_REPORT" || exit 2
  echo "Disposable checkpoint recorded. Deployment paused for external reboot."
  kill -STOP "$$"
  # Resuming without reboot must not silently finish the interrupted deployment.
  exit 2
}
trap 'vm_checkpoint "$BASH_COMMAND"' DEBUG
