"""Synthetic, local-only compatibility policies; no real account evidence."""
import copy,hashlib,json,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
import xhs_extension_compatibility as compatibility


class Compatibility(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.root=Path(self.temp.name)
        self.package=self.root/'synthetic-package.js'
        self.package.write_bytes(b'// Synthetic adapter revision 1.0.27\n')
        self.binding={key:'synthetic-'+key for key in compatibility.KEYS}
        self.binding['extension_version']='1.0.26'
        self.policy={
            'authority_projection':copy.deepcopy(self.binding),
            'to_version':'1.0.27',
            'source_files':[{'file':str(self.package),'sha256':self.sha(self.package)}]}
        self.policy_file=self.root/'synthetic-policy.json'
        self.pin_policy()

    def tearDown(self):
        self.temp.cleanup()

    @staticmethod
    def sha(path):
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def pin_policy(self):
        self.policy_file.write_text(json.dumps(self.policy))
        self.binding['extension_compatibility_policy']={
            'file':str(self.policy_file),'sha256':self.sha(self.policy_file)}

    def test_no_reference_returns_original_without_any_external_read(self):
        del self.binding['extension_compatibility_policy']
        for version in ('1.0.26','1.0.27','unrelated-version'):
            self.binding['extension_version']=version
            with patch.object(Path,'read_bytes',side_effect=AssertionError('No external policy read')):
                self.assertEqual(compatibility.effective_extension_version(self.binding),version)

    def test_only_exact_pinned_binding_and_package_enable_compatibility(self):
        self.assertEqual(compatibility.effective_extension_version(self.binding),'1.0.27')

    def test_explicit_malformed_references_fail_closed(self):
        for reference in (None,{},[],{'file':str(self.policy_file)},
                          {'file':'relative.json','sha256':'a'*64},
                          {'file':str(self.policy_file),'sha256':'bad'},
                          {'file':str(self.policy_file),'sha256':'a'*64,'extra':True}):
            with self.subTest(reference=reference):
                self.binding['extension_compatibility_policy']=reference
                with self.assertRaises(RuntimeError):
                    compatibility.effective_extension_version(self.binding)

    def test_missing_policy_fails_closed(self):
        self.policy_file.unlink()
        with self.assertRaises(RuntimeError):compatibility.effective_extension_version(self.binding)

    def test_policy_symlink_is_rejected(self):
        link=self.root/'policy-link.json';link.symlink_to(self.policy_file)
        self.binding['extension_compatibility_policy']['file']=str(link)
        with self.assertRaises(RuntimeError):compatibility.effective_extension_version(self.binding)

    def test_policy_byte_changes_invalidate_sha(self):
        self.policy_file.write_bytes(self.policy_file.read_bytes()+b' ')
        with self.assertRaisesRegex(RuntimeError,'POLICY_CHANGED'):
            compatibility.effective_extension_version(self.binding)

    def test_even_sha_pinned_invalid_json_is_rejected(self):
        self.policy_file.write_bytes(b'not JSON')
        self.binding['extension_compatibility_policy']['sha256']=self.sha(self.policy_file)
        with self.assertRaises(ValueError):compatibility.effective_extension_version(self.binding)

    def test_every_binding_fingerprint_field_is_preserved(self):
        original=copy.deepcopy(self.binding)
        for key in compatibility.KEYS:
            with self.subTest(key=key):
                self.binding=copy.deepcopy(original);self.binding[key]='changed'
                with self.assertRaisesRegex(RuntimeError,'BINDING_CHANGED'):
                    compatibility.effective_extension_version(self.binding)

    def test_source_byte_changes_are_rejected(self):
        self.package.write_bytes(b'changed package')
        with self.assertRaisesRegex(RuntimeError,'SOURCE_CHANGED'):
            compatibility.effective_extension_version(self.binding)

    def test_source_symlink_is_rejected_even_when_bytes_match(self):
        link=self.root/'source-link.js';link.symlink_to(self.package)
        self.policy['source_files'][0]['file']=str(link);self.pin_policy()
        with self.assertRaisesRegex(RuntimeError,'SOURCE_CHANGED'):
            compatibility.effective_extension_version(self.binding)

    def test_missing_source_cannot_enable_compatibility(self):
        self.package.unlink()
        with self.assertRaises(OSError):compatibility.effective_extension_version(self.binding)

    def test_no_other_source_or_target_extension_version_is_allowed(self):
        for source,target in (('1.0.25','1.0.27'),('1.0.27','1.0.27'),('1.0.26','1.0.28')):
            with self.subTest(source=source,target=target):
                self.binding['extension_version']=source
                self.policy['authority_projection']={k:self.binding.get(k) for k in compatibility.KEYS}
                self.policy['to_version']=target;self.pin_policy()
                with self.assertRaisesRegex(RuntimeError,'SCOPE_CHANGED'):
                    compatibility.effective_extension_version(self.binding)


if __name__=='__main__':unittest.main()
