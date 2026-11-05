# Copyright (C) 2026 The Android Open Source Project
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#       http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Tests for `vendor_dlkm_image`."""

load("@bazel_skylib//lib:unittest.bzl", "analysistest", "asserts")
load(
    "//build/kernel/kleaf:kernel.bzl",
    "ddk_module",
    "kernel_build",
    "kernel_modules_install",
    "system_dlkm_image",
    "vendor_dlkm_image",
)
load("//build/kernel/kleaf/tests:failure_test.bzl", "failure_test")
load("//build/kernel/kleaf/tests:test_utils.bzl", "test_utils")

def _find_file_write_content(actions, basename):
    for action in actions:
        if action.mnemonic == "FileWrite":
            for out in action.outputs.to_list():
                if out.basename == basename:
                    return action.content
    return None

def _vendor_dlkm_dedup_test_impl(ctx):
    env = analysistest.begin(ctx)
    actions = analysistest.target_actions(env)
    action = test_utils.find_action(env, "VendorDlkmImage")
    script = test_utils.get_shell_script(env, action)
    for expected in ctx.attr.expected_substrings:
        asserts.true(
            env,
            expected in script,
            msg = "expected substring '{}' not found in VendorDlkmImage script".format(expected),
        )
    for unexpected in ctx.attr.unexpected_substrings:
        asserts.false(
            env,
            unexpected in script,
            msg = "unexpected substring '{}' found in VendorDlkmImage script".format(unexpected),
        )
    if ctx.attr.check_device_module_basenames:
        content = _find_file_write_content(actions, "device_module_basenames.txt")
        asserts.true(
            env,
            content != None,
            msg = "expected FileWrite action for device_module_basenames.txt",
        )
        asserts.equals(
            env,
            ctx.attr.expected_device_module_basenames,
            [line for line in content.splitlines() if line],
        )
    if ctx.attr.check_external_module_basenames:
        content = _find_file_write_content(actions, "external_module_basenames.txt")
        asserts.true(
            env,
            content != None,
            msg = "expected FileWrite action for external_module_basenames.txt",
        )
        asserts.equals(
            env,
            ctx.attr.expected_external_module_basenames,
            [line for line in content.splitlines() if line],
        )
    return analysistest.end(env)

_vendor_dlkm_dedup_test = analysistest.make(
    impl = _vendor_dlkm_dedup_test_impl,
    attrs = {
        "expected_substrings": attr.string_list(),
        "unexpected_substrings": attr.string_list(),
        "check_device_module_basenames": attr.bool(),
        "expected_device_module_basenames": attr.string_list(),
        "check_external_module_basenames": attr.bool(),
        "expected_external_module_basenames": attr.string_list(),
    },
)

def vendor_dlkm_image_test(name):
    """Define tests for `vendor_dlkm_image`.

    Args:
      name: Name of this test suite.
    """
    kernel_build(
        name = name + "_build",
        outs = ["System.map"],
        module_outs = ["drivers/block/zram/zram.ko"],
        tags = ["manual"],
    )
    kernel_modules_install(
        name = name + "_modules_install",
        kernel_build = name + "_build",
        tags = ["manual"],
    )
    system_dlkm_image(
        name = name + "_system_dlkm",
        kernel_modules_install = name + "_modules_install",
        build_flatten = True,
        fs_types = ["erofs"],
        tags = ["manual"],
    )

    tests = []

    vendor_dlkm_image(
        name = name + "_with_system_dlkm_image",
        kernel_modules_install = name + "_modules_install",
        build_flatten = True,
        dedup_dlkm_modules = True,
        system_dlkm_image = name + "_system_dlkm",
        tags = ["manual"],
    )
    _vendor_dlkm_dedup_test(
        name = name + "_system_dlkm_attr_test",
        target_under_test = name + "_with_system_dlkm_image",
        expected_substrings = [
            "${DIST_DIR}/system_dlkm.modules",
            'cat "${DIST_DIR}/system_dlkm.modules" >> "${DIST_DIR}/modules.load"',
            "VENDOR_DLKM_GEN_FLATTEN_IMAGE=1",
        ],
        unexpected_substrings = [
            "system_dlkm.modules.load",
            "external_module_basenames.txt",
        ],
        # Single-kernel build (base_kernel == None) must not treat kernel_build.module_outs
        # as device overrides of system_dlkm.
        check_device_module_basenames = True,
        expected_device_module_basenames = [],
    )
    tests.append(name + "_system_dlkm_attr_test")

    vendor_dlkm_image(
        name = name + "_with_base_system_dlkm_image",
        kernel_modules_install = name + "_modules_install",
        build_flatten = True,
        dedup_dlkm_modules = True,
        base_system_dlkm_image = name + "_system_dlkm",
        tags = ["manual"],
    )
    _vendor_dlkm_dedup_test(
        name = name + "_base_system_dlkm_attr_test",
        target_under_test = name + "_with_base_system_dlkm_image",
        expected_substrings = [
            "${DIST_DIR}/system_dlkm.modules",
            "VENDOR_DLKM_GEN_FLATTEN_IMAGE=1",
        ],
    )
    tests.append(name + "_base_system_dlkm_attr_test")

    vendor_dlkm_image(
        name = name + "_without_dedup",
        kernel_modules_install = name + "_modules_install",
        build_flatten = True,
        dedup_dlkm_modules = False,
        system_dlkm_image = name + "_system_dlkm",
        tags = ["manual"],
    )
    _vendor_dlkm_dedup_test(
        name = name + "_no_dedup_test",
        target_under_test = name + "_without_dedup",
        expected_substrings = [
            "VENDOR_DLKM_GEN_FLATTEN_IMAGE=1",
        ],
        unexpected_substrings = [
            "${DIST_DIR}/system_dlkm.modules",
        ],
    )
    tests.append(name + "_no_dedup_test")

    vendor_dlkm_image(
        name = name + "_missing_system_dlkm",
        kernel_modules_install = name + "_modules_install",
        build_flatten = True,
        dedup_dlkm_modules = True,
        tags = ["manual"],
    )
    failure_test(
        name = name + "_missing_system_dlkm_test",
        target_under_test = name + "_missing_system_dlkm",
        error_message_substrs = [
            "With dedup_dlkm_modules, either build_system_dlkm or base_system_dlkm_image must be set",
        ],
    )
    tests.append(name + "_missing_system_dlkm_test")

    # DDK-only build against base kernel (base_kernel == None) with external module:
    # external module is excluded from system_dlkm, while base kernel's module_outs are not.
    ddk_module(
        name = name + "_single_ext_mod",
        out = "ext_only.ko",
        kernel_build = name + "_build",
        srcs = [],
        tags = ["manual"],
    )
    kernel_modules_install(
        name = name + "_single_ext_modules_install",
        kernel_build = name + "_build",
        kernel_modules = [name + "_single_ext_mod"],
        tags = ["manual"],
    )
    vendor_dlkm_image(
        name = name + "_single_ext_vendor_dlkm",
        kernel_modules_install = name + "_single_ext_modules_install",
        build_flatten = True,
        dedup_dlkm_modules = True,
        base_system_dlkm_image = name + "_system_dlkm",
        tags = ["manual"],
    )
    _vendor_dlkm_dedup_test(
        name = name + "_single_ext_override_test",
        target_under_test = name + "_single_ext_vendor_dlkm",
        expected_substrings = [
            "device_module_basenames.txt",
            "external_module_basenames.txt",
            "overridden_ko",
        ],
        check_device_module_basenames = True,
        expected_device_module_basenames = ["ext_only.ko"],
        check_external_module_basenames = True,
        expected_external_module_basenames = ["ext_only.ko"],
    )
    tests.append(name + "_single_ext_override_test")

    # Mixed build (base_kernel set) with both in-tree and external overrides:
    # both in-tree module_outs and external modules are recorded in device_module_basenames.txt,
    # while only external modules are recorded in external_module_basenames.txt.
    kernel_build(
        name = name + "_device_build",
        base_kernel = name + "_build",
        outs = [],
        module_outs = ["drivers/block/zram/zram.ko"],
        module_implicit_outs = ["drivers/misc/vendor_implicit.ko"],
        tags = ["manual"],
    )
    ddk_module(
        name = name + "_device_ext_mod",
        out = "ext_override.ko",
        kernel_build = name + "_device_build",
        srcs = [],
        tags = ["manual"],
    )
    kernel_modules_install(
        name = name + "_device_modules_install",
        kernel_build = name + "_device_build",
        kernel_modules = [name + "_device_ext_mod"],
        tags = ["manual"],
    )
    system_dlkm_image(
        name = name + "_device_system_dlkm",
        base = name + "_system_dlkm",
        kernel_modules_install = name + "_device_modules_install",
        build_flatten = True,
        fs_types = ["erofs"],
        tags = ["manual"],
    )
    vendor_dlkm_image(
        name = name + "_device_vendor_dlkm",
        kernel_modules_install = name + "_device_modules_install",
        build_flatten = True,
        dedup_dlkm_modules = True,
        system_dlkm_image = name + "_device_system_dlkm",
        tags = ["manual"],
    )
    _vendor_dlkm_dedup_test(
        name = name + "_mixed_build_overrides_test",
        target_under_test = name + "_device_vendor_dlkm",
        expected_substrings = [
            "device_module_basenames.txt",
            "external_module_basenames.txt",
            "${DIST_DIR}/system_dlkm.modules",
            'cat "${DIST_DIR}/system_dlkm.modules" >> "${DIST_DIR}/modules.load"',
        ],
        check_device_module_basenames = True,
        expected_device_module_basenames = [
            "ext_override.ko",
            "vendor_implicit.ko",
            "zram.ko",
        ],
        check_external_module_basenames = True,
        expected_external_module_basenames = ["ext_override.ko"],
    )
    tests.append(name + "_mixed_build_overrides_test")

    native.test_suite(
        name = name,
        tests = tests,
    )
