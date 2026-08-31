import os
import platform
import re

from virttest import data_dir
from virttest import utils_kdump
from virttest import virsh
from virttest.libvirt_xml import vm_xml

from provider.migration import base_steps


def run(test, params, env):
    """
    Verify that migration can succeed when cpu mode is configured.

    :param test: test object
    :param params: Dictionary with the test parameters
    :param env: Dictionary with test environment.
    """
    def get_host_cpu_model():
        """Get host CPU model in lowercase."""
        try:
            caps_xml = virsh.capabilities()
            match = re.search(r'<model>(\w+)</model>', caps_xml)
            if match:
                model = match.group(1).lower()
                test.log.info("Detected CPU model: %s", model)
                return model
        except Exception as e:
            test.log.warning("Failed to detect CPU model: %s", e)
        return None

    def setup_test():
        """
        Setup steps
        """
        cpu_mode = params.get("cpu_mode")
        migration_option = params.get("migration_option")

        test.log.info("Setup steps.")
        migration_obj.setup_connection()
        vmxml = vm_xml.VMXML.new_from_dumpxml(vm_name)
        if cpu_mode == "host_model":
            vm_attrs = eval(params.get('vm_attrs', '{}'))
            arch = platform.machine()
            if arch in ['ppc64', 'ppc64le'] and 'cpu' in vm_attrs:
                host_model = get_host_cpu_model()
                if host_model:
                    vm_attrs['cpu']['model'] = host_model
                    vm_attrs['cpu']['fallback'] = params.get('model_fallback', 'forbid')
                    test.log.info("Power arch - added CPU model: %s", host_model)
            vmxml.setup_attrs(**vm_attrs)
            vmxml.sync()
        else:
            base_steps.sync_cpu_for_mig(params)

        # If kdump will be triggered after migration, ensure the VM restarts
        # after a crash so we can verify the vmcore and wait for reboot.
        if params.get("post_migration_kdump") == "yes":
            vmxml = vm_xml.VMXML.new_from_dumpxml(vm_name)
            vmxml.on_crash = "restart"
            vmxml.sync()
            test.log.info("Set on_crash=restart for kdump test.")

        if not vm.is_alive():
            vm.start()
            vm.wait_for_login().close()

        if migration_option == "with_xml":
            xmlfile = os.path.join(data_dir.get_tmp_dir(), '%s.xml' % vm_name)
            virsh.dumpxml(vm_name, extra="--migratable", to_file=xmlfile, ignore_status=False)
            params.update({"virsh_migrate_extra": f"--xml {xmlfile}"})

    def _get_vmcores_via_serial(session):
        """List vmcore files using an already-open serial session."""
        status, output = session.cmd_status_output(
            "find /var/crash/ -type f -name vmcore 2>/dev/null | sort",
            timeout=60,
        )
        if status:
            return []
        return [v for v in output.split() if v]

    def verify_kdump_after_migration():
        """
        Trigger kdump on the guest after successful migration and verify
        a new vmcore is created.

        All guest access uses wait_for_serial_login() which connects via the
        virsh serial console (qemu+tcp://dest/system console <vm>).  This
        bypasses the source-side ARP / address-cache verification that
        wait_for_login() performs and that always fails after live migration
        because the VM MAC is no longer reachable on the source host virbr0.

        Guest must have on_crash=restart in its XML (set in setup_test()) so
        it reboots after kdump allowing vmcore presence to be confirmed.
        """
        test.log.info("Triggering kdump on guest '%s' after migration.", vm_name)

        # Pre-crash vmcore snapshot via serial console
        pre_session = vm.wait_for_serial_login(timeout=240)
        try:
            pre_vmcores = _get_vmcores_via_serial(pre_session)
            test.log.info("Pre-crash vmcores: %s", pre_vmcores)
        finally:
            pre_session.close()

        # Trigger kernel panic via serial session
        crash_session = vm.wait_for_serial_login(timeout=240)
        utils_kdump.trigger_crash(
            vm,
            session=crash_session,
            wait_time=300,
            test=test,
        )
        test.log.info("Kernel panic triggered; waiting for guest to reboot.")

        # Wait for reboot via serial (on_crash=restart ensures reboot)
        vm.wait_for_serial_login(timeout=360).close()
        test.log.info("Guest rebooted after kdump.")

        # Post-crash vmcore list via serial
        post_session = vm.wait_for_serial_login(timeout=240)
        try:
            post_vmcores = _get_vmcores_via_serial(post_session)
            test.log.info("Post-crash vmcores: %s", post_vmcores)
        finally:
            post_session.close()

        new_vmcores = [v for v in post_vmcores if v not in pre_vmcores]
        if not new_vmcores:
            test.fail(
                "No new vmcore found in guest '%s' after kdump. "
                "pre=%s post=%s" % (vm_name, pre_vmcores, post_vmcores)
            )
        test.log.info("kdump verified - new vmcore(s): %s", new_vmcores)

    vm_name = params.get("migrate_main_vm")

    vm = env.get_vm(vm_name)
    migration_obj = base_steps.MigrationBase(test, vm, params)

    try:
        setup_test()
        migration_obj.run_migration()
        migration_obj.verify_default()
        if params.get("post_migration_kdump") == "yes":
            verify_kdump_after_migration()
    finally:
        migration_obj.cleanup_connection()
