"""Tests for the certificates Python API. """


import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta
from unittest import mock
from unittest.mock import patch

import ddt
import pytz
from config_models.models import cache
from django.conf import settings
from django.test import RequestFactory, TestCase
from django.test.utils import override_settings
from django.urls import reverse
from django.utils import timezone
from freezegun import freeze_time
from opaque_keys.edx.keys import CourseKey
from opaque_keys.edx.locator import CourseLocator
from testfixtures import LogCapture
from xmodule.modulestore.tests.django_utils import ModuleStoreTestCase, SharedModuleStoreTestCase
from xmodule.modulestore.tests.factories import CourseFactory

from common.djangoapps.course_modes.models import CourseMode
from common.djangoapps.course_modes.tests.factories import CourseModeFactory
from common.djangoapps.student.tests.factories import (
    CourseEnrollmentFactory,
    GlobalStaffFactory,
    UserFactory
)
from common.djangoapps.util.testing import EventTestMixin
from lms.djangoapps.certificates.api import (
    can_be_added_to_allowlist,
    cert_generation_enabled,
    certificate_downloadable_status,
    create_certificate_invalidation_entry,
    create_or_update_certificate_allowlist_entry,
    example_certificates_status,
    generate_certificate_task,
    generate_example_certificates,
    get_allowlist_entry,
    get_allowlisted_users,
    get_certificate_footer_context,
    get_certificate_for_user,
    get_certificate_for_user_id,
    get_certificate_header_context,
    get_certificate_invalidation_entry,
    get_certificate_url,
    get_certificates_for_user,
    get_certificates_for_user_by_course_keys,
    is_certificate_invalidated,
    is_on_allowlist,
    remove_allowlist_entry,
    set_cert_generation_enabled,
    certificate_status_for_student,
)
from lms.djangoapps.certificates.models import (
    CertificateGenerationConfiguration,
    CertificateStatuses,
    ExampleCertificate,
    GeneratedCertificate,
)
from lms.djangoapps.certificates.queue import XQueueAddToQueueError, XQueueCertInterface
from lms.djangoapps.certificates.tests.factories import (
    CertificateAllowlistFactory,
    GeneratedCertificateFactory,
    CertificateInvalidationFactory
)
from lms.djangoapps.certificates.tests.test_generation_handler import ID_VERIFIED_METHOD, PASSING_GRADE_METHOD
from openedx.core.djangoapps.content.course_overviews.tests.factories import CourseOverviewFactory
from openedx.core.djangoapps.site_configuration.tests.test_util import with_site_configuration

CAN_GENERATE_METHOD = 'lms.djangoapps.certificates.generation_handler._can_generate_regular_certificate'
FEATURES_WITH_CERTS_ENABLED = settings.FEATURES.copy()
FEATURES_WITH_CERTS_ENABLED['CERTIFICATES_HTML_VIEW'] = True


class WebCertificateTestMixin:
    """
    Mixin with helpers for testing Web Certificates.
    """
    @contextmanager
    def _mock_queue(self, is_successful=True):
        """
        Mock the "send to XQueue" method to return either success or an error.
        """
        symbol = 'capa.xqueue_interface.XQueueInterface.send_to_queue'
        with patch(symbol) as mock_send_to_queue:
            if is_successful:
                mock_send_to_queue.return_value = (0, "Successfully queued")
            else:
                mock_send_to_queue.side_effect = XQueueAddToQueueError(1, self.ERROR_REASON)

            yield mock_send_to_queue

    def _setup_course_certificate(self):
        """
        Creates certificate configuration for course
        """
        certificates = [
            {
                'id': 1,
                'name': 'Test Certificate Name',
                'description': 'Test Certificate Description',
                'course_title': 'tes_course_title',
                'signatories': [],
                'version': 1,
                'is_active': True
            }
        ]
        self.course.certificates = {'certificates': certificates}
        self.course.cert_html_view_enabled = True
        self.course.save()
        self.store.update_item(self.course, self.user.id)


@ddt.ddt
class CertificateDownloadableStatusTests(WebCertificateTestMixin, ModuleStoreTestCase):
    """Tests for the `certificate_downloadable_status` helper function. """
    ENABLED_SIGNALS = ['course_published']

    def setUp(self):
        super().setUp()

        self.student = UserFactory()
        self.student_no_cert = UserFactory()
        self.course = CourseFactory.create(
            org='edx',
            number='verified',
            display_name='Verified Course',
            end=datetime.now(pytz.UTC),
            self_paced=False,
            certificate_available_date=datetime.now(pytz.UTC) - timedelta(days=2)
        )

        GeneratedCertificateFactory.create(
            user=self.student,
            course_id=self.course.id,
            status=CertificateStatuses.downloadable,
            mode='verified'
        )

        self.request_factory = RequestFactory()

    def test_cert_status_with_generating(self):
        cert_user = UserFactory()
        GeneratedCertificateFactory.create(
            user=cert_user,
            course_id=self.course.id,
            status=CertificateStatuses.generating,
            mode='verified'
        )
        assert certificate_downloadable_status(cert_user, self.course.id) ==\
               {'is_downloadable': False,
                'is_generating': True,
                'is_unverified': False,
                'download_url': None,
                'uuid': None}

    def test_cert_status_with_error(self):
        cert_user = UserFactory()
        GeneratedCertificateFactory.create(
            user=cert_user,
            course_id=self.course.id,
            status=CertificateStatuses.error,
            mode='verified'
        )

        assert certificate_downloadable_status(cert_user, self.course.id) ==\
               {'is_downloadable': False,
                'is_generating': True,
                'is_unverified': False,
                'download_url': None,
                'uuid': None}

    def test_without_cert(self):
        assert certificate_downloadable_status(self.student_no_cert, self.course.id) ==\
               {'is_downloadable': False,
                'is_generating': False,
                'is_unverified': False,
                'download_url': None,
                'uuid': None}

    def verify_downloadable_pdf_cert(self):
        """
        Verifies certificate_downloadable_status returns the
        correct response for PDF certificates.
        """
        cert_user = UserFactory()
        cert = GeneratedCertificateFactory.create(
            user=cert_user,
            course_id=self.course.id,
            status=CertificateStatuses.downloadable,
            mode='verified',
            download_url='www.google.com',
        )

        assert certificate_downloadable_status(cert_user, self.course.id) ==\
               {'is_downloadable': True,
                'is_generating': False,
                'is_unverified': False,
                'download_url': 'www.google.com',
                'is_pdf_certificate': True,
                'uuid': cert.verify_uuid}

    @patch.dict(settings.FEATURES, {'CERTIFICATES_HTML_VIEW': True})
    def test_pdf_cert_with_html_enabled(self):
        self.verify_downloadable_pdf_cert()

    def test_pdf_cert_with_html_disabled(self):
        self.verify_downloadable_pdf_cert()

    @patch.dict(settings.FEATURES, {'CERTIFICATES_HTML_VIEW': True})
    def test_with_downloadable_web_cert(self):
        cert_status = certificate_status_for_student(self.student, self.course.id)
        assert certificate_downloadable_status(self.student, self.course.id) ==\
               {'is_downloadable': True,
                'is_generating': False,
                'is_unverified': False,
                'download_url': f'/certificates/{cert_status["uuid"]}',
                'is_pdf_certificate': False,
                'uuid': cert_status['uuid']}

    @ddt.data(
        (False, timedelta(days=2), False, True),
        (False, -timedelta(days=2), True, None),
        (True, timedelta(days=2), True, None)
    )
    @ddt.unpack
    @patch.dict(settings.FEATURES, {'CERTIFICATES_HTML_VIEW': True})
    def test_cert_api_return(self, self_paced, cert_avail_delta, cert_downloadable_status, earned_but_not_available):
        """
        Test 'downloadable status'
        """
        cert_avail_date = datetime.now(pytz.UTC) + cert_avail_delta
        self.course.self_paced = self_paced
        self.course.certificate_available_date = cert_avail_date
        self.course.save()

        self._setup_course_certificate()

        downloadable_status = certificate_downloadable_status(self.student, self.course.id)
        assert downloadable_status['is_downloadable'] == cert_downloadable_status
        assert downloadable_status.get('earned_but_not_available') == earned_but_not_available


@ddt.ddt
class CertificateIsInvalid(WebCertificateTestMixin, ModuleStoreTestCase):
    """Tests for the `is_certificate_invalid` helper function. """

    def setUp(self):
        super().setUp()

        self.student = UserFactory()
        self.course = CourseFactory.create(
            org='edx',
            number='verified',
            display_name='Verified Course'
        )
        self.course_overview = CourseOverviewFactory.create(
            id=self.course.id
        )
        self.global_staff = GlobalStaffFactory()
        self.request_factory = RequestFactory()

    def test_method_with_no_certificate(self):
        """ Test the case when there is no certificate for a user for a specific course. """
        course = CourseFactory.create(
            org='edx',
            number='honor',
            display_name='Course 1'
        )
        # Also check query count for 'is_certificate_invalid' method.
        with self.assertNumQueries(1):
            assert not is_certificate_invalidated(self.student, course.id)

    @ddt.data(
        CertificateStatuses.generating,
        CertificateStatuses.downloadable,
        CertificateStatuses.notpassing,
        CertificateStatuses.error,
        CertificateStatuses.unverified,
        CertificateStatuses.deleted,
        CertificateStatuses.unavailable,
    )
    def test_method_with_invalidated_cert(self, status):
        """ Verify that if certificate is marked as invalid than method will return
        True. """
        generated_cert = self._generate_cert(status)
        self._invalidate_certificate(generated_cert, True)
        assert is_certificate_invalidated(self.student, self.course.id)

    @ddt.data(
        CertificateStatuses.generating,
        CertificateStatuses.downloadable,
        CertificateStatuses.notpassing,
        CertificateStatuses.error,
        CertificateStatuses.unverified,
        CertificateStatuses.deleted,
        CertificateStatuses.unavailable,
    )
    def test_method_with_inactive_invalidated_cert(self, status):
        """ Verify that if certificate is valid but it's invalidated status is
        false than method will return false. """
        generated_cert = self._generate_cert(status)
        self._invalidate_certificate(generated_cert, False)
        assert not is_certificate_invalidated(self.student, self.course.id)

    @ddt.data(
        CertificateStatuses.generating,
        CertificateStatuses.downloadable,
        CertificateStatuses.notpassing,
        CertificateStatuses.error,
        CertificateStatuses.unverified,
        CertificateStatuses.deleted,
        CertificateStatuses.unavailable,
    )
    def test_method_with_all_statues(self, status):
        """ Verify method return True if certificate has valid status but it is
        marked as invalid in CertificateInvalidation table. """

        certificate = self._generate_cert(status)
        CertificateInvalidationFactory.create(
            generated_certificate=certificate,
            invalidated_by=self.global_staff,
            active=True
        )
        # Also check query count for 'is_certificate_invalid' method.
        with self.assertNumQueries(2):
            assert is_certificate_invalidated(self.student, self.course.id)

    def _invalidate_certificate(self, certificate, active):
        """ Dry method to mark certificate as invalid. """
        CertificateInvalidationFactory.create(
            generated_certificate=certificate,
            invalidated_by=self.global_staff,
            active=active
        )
        # Invalidate user certificate
        certificate.invalidate()
        assert not certificate.is_valid()

    def _generate_cert(self, status):
        """ Dry method to generate certificate. """
        return GeneratedCertificateFactory.create(
            user=self.student,
            course_id=self.course.id,
            status=status,
            mode='verified'
        )


class CertificateGetTests(SharedModuleStoreTestCase):
    """Tests for the `test_get_certificate_for_user` helper function. """
    now = timezone.now()

    @classmethod
    def setUpClass(cls):
        cls.freezer = freeze_time(cls.now)
        cls.freezer.start()

        super().setUpClass()
        cls.student = UserFactory()
        cls.student_no_cert = UserFactory()
        cls.uuid = uuid.uuid4().hex
        cls.nonexistent_course_id = CourseKey.from_string('course-v1:some+fake+course')
        cls.web_cert_course = CourseFactory.create(
            org='edx',
            number='verified_1',
            display_name='Verified Course 1',
            cert_html_view_enabled=True
        )
        cls.pdf_cert_course = CourseFactory.create(
            org='edx',
            number='verified_2',
            display_name='Verified Course 2',
            cert_html_view_enabled=False
        )
        cls.no_cert_course = CourseFactory.create(
            org='edx',
            number='verified_3',
            display_name='Verified Course 3',
        )
        # certificate for the first course
        GeneratedCertificateFactory.create(
            user=cls.student,
            course_id=cls.web_cert_course.id,
            status=CertificateStatuses.downloadable,
            mode='verified',
            download_url='www.google.com',
            grade="0.88",
            verify_uuid=cls.uuid,
        )
        # certificate for the second course
        GeneratedCertificateFactory.create(
            user=cls.student,
            course_id=cls.pdf_cert_course.id,
            status=CertificateStatuses.downloadable,
            mode='honor',
            download_url='www.gmail.com',
            grade="0.99",
            verify_uuid=cls.uuid,
        )
        # certificate for a course that will be deleted
        GeneratedCertificateFactory.create(
            user=cls.student,
            course_id=cls.nonexistent_course_id,
            status=CertificateStatuses.downloadable
        )

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        cls.freezer.stop()

    def test_get_certificate_for_user(self):
        """
        Test to get a certificate for a user for a specific course.
        """
        cert = get_certificate_for_user(self.student.username, self.web_cert_course.id)

        assert cert['username'] == self.student.username
        assert cert['course_key'] == self.web_cert_course.id
        assert cert['created'] == self.now
        assert cert['type'] == CourseMode.VERIFIED
        assert cert['status'] == CertificateStatuses.downloadable
        assert cert['grade'] == '0.88'
        assert cert['is_passing'] is True
        assert cert['download_url'] == 'www.google.com'

    def test_get_certificate_for_user_id(self):
        """
        Test to get a certificate for a user id for a specific course.
        """
        cert = get_certificate_for_user_id(self.student, self.web_cert_course.id)

        assert cert is not None
        assert cert.course_id == self.web_cert_course.id
        assert cert.mode == CourseMode.VERIFIED
        assert cert.status == CertificateStatuses.downloadable
        assert cert.grade == '0.88'

    def test_get_certificates_for_user(self):
        """
        Test to get all the certificates for a user
        """
        certs = get_certificates_for_user(self.student.username)
        assert len(certs) == 2
        assert certs[0]['username'] == self.student.username
        assert certs[1]['username'] == self.student.username
        assert certs[0]['course_key'] == self.web_cert_course.id
        assert certs[1]['course_key'] == self.pdf_cert_course.id
        assert certs[0]['created'] == self.now
        assert certs[1]['created'] == self.now
        assert certs[0]['type'] == CourseMode.VERIFIED
        assert certs[1]['type'] == CourseMode.HONOR
        assert certs[0]['status'] == CertificateStatuses.downloadable
        assert certs[1]['status'] == CertificateStatuses.downloadable
        assert certs[0]['is_passing'] is True
        assert certs[1]['is_passing'] is True
        assert certs[0]['grade'] == '0.88'
        assert certs[1]['grade'] == '0.99'
        assert certs[0]['download_url'] == 'www.google.com'
        assert certs[1]['download_url'] == 'www.gmail.com'

    def test_get_certificates_for_user_by_course_keys(self):
        """
        Test to get certificates for a user for certain course keys,
        in a dictionary indexed by those course keys.
        """
        certs = get_certificates_for_user_by_course_keys(
            user=self.student,
            course_keys={self.web_cert_course.id, self.no_cert_course.id},
        )
        assert set(certs.keys()) == {self.web_cert_course.id}
        cert = certs[self.web_cert_course.id]
        assert cert['username'] == self.student.username
        assert cert['course_key'] == self.web_cert_course.id
        assert cert['download_url'] == 'www.google.com'

    def test_no_certificate_for_user(self):
        """
        Test the case when there is no certificate for a user for a specific course.
        """
        assert get_certificate_for_user(self.student_no_cert.username, self.web_cert_course.id) is None

    def test_no_certificates_for_user(self):
        """
        Test the case when there are no certificates for a user.
        """
        assert get_certificates_for_user(self.student_no_cert.username) == []

    @patch.dict(settings.FEATURES, {'CERTIFICATES_HTML_VIEW': True})
    def test_get_web_certificate_url(self):
        """
        Test the get_certificate_url with a web cert course
        """
        expected_url = reverse(
            'certificates:render_cert_by_uuid',
            kwargs=dict(certificate_uuid=self.uuid)
        )
        cert_url = get_certificate_url(
            user_id=self.student.id,
            course_id=self.web_cert_course.id,
            uuid=self.uuid
        )
        assert expected_url == cert_url

        expected_url = reverse(
            'certificates:render_cert_by_uuid',
            kwargs=dict(certificate_uuid=self.uuid)
        )

        cert_url = get_certificate_url(
            user_id=self.student.id,
            course_id=self.web_cert_course.id,
            uuid=self.uuid
        )
        assert expected_url == cert_url

    @patch.dict(settings.FEATURES, {'CERTIFICATES_HTML_VIEW': True})
    def test_get_pdf_certificate_url(self):
        """
        Test the get_certificate_url with a pdf cert course
        """
        cert_url = get_certificate_url(
            user_id=self.student.id,
            course_id=self.pdf_cert_course.id,
            uuid=self.uuid
        )
        assert 'www.gmail.com' == cert_url

    def test_get_certificate_with_deleted_course(self):
        """
        Test the case when there is a certificate but the course was deleted.
        """
        assert get_certificate_for_user(self.student.username, self.nonexistent_course_id) is None


class GenerateUserCertificatesTest(ModuleStoreTestCase):
    """Tests for generating certificates for students. """

    def setUp(self):
        super().setUp()

        self.user = UserFactory()
        self.course_run = CourseFactory()
        self.course_run_key = self.course_run.id  # pylint: disable=no-member
        self.enrollment = CourseEnrollmentFactory(
            user=self.user,
            course_id=self.course_run_key,
            is_active=True,
            mode=CourseMode.VERIFIED,
        )

    @patch.dict(settings.FEATURES, {'CERTIFICATES_HTML_VIEW': False})
    def test_cert_url_empty_with_invalid_certificate(self):
        """
        Test certificate url is empty if html view is not enabled and certificate is not yet generated
        """
        url = get_certificate_url(self.user.id, self.course_run_key)
        assert url == ''

    @patch.dict(settings.FEATURES, {'CERTIFICATES_HTML_VIEW': True})
    def test_generation(self):
        """
        Test that a cert is successfully generated
        """
        cert = get_certificate_for_user_id(self.user.id, self.course_run_key)
        assert not cert

        with mock.patch(PASSING_GRADE_METHOD, return_value=True):
            with mock.patch(ID_VERIFIED_METHOD, return_value=True):
                generate_certificate_task(self.user, self.course_run_key)

                cert = get_certificate_for_user_id(self.user.id, self.course_run_key)
                assert cert.status == CertificateStatuses.downloadable
                assert cert.mode == CourseMode.VERIFIED


@ddt.ddt
class CertificateGenerationEnabledTest(EventTestMixin, TestCase):
    """Test enabling/disabling self-generated certificates for a course. """

    COURSE_KEY = CourseLocator(org='test', course='test', run='test')

    def setUp(self):  # pylint: disable=arguments-differ
        super().setUp('lms.djangoapps.certificates.api.tracker')

        # Since model-based configuration is cached, we need
        # to clear the cache before each test.
        cache.clear()

    @ddt.data(
        (None, None, False),
        (False, None, False),
        (False, True, False),
        (True, None, False),
        (True, False, False),
        (True, True, True)
    )
    @ddt.unpack
    def test_cert_generation_enabled(self, is_feature_enabled, is_course_enabled, expect_enabled):
        if is_feature_enabled is not None:
            CertificateGenerationConfiguration.objects.create(enabled=is_feature_enabled)

        if is_course_enabled is not None:
            set_cert_generation_enabled(self.COURSE_KEY, is_course_enabled)
            cert_event_type = 'enabled' if is_course_enabled else 'disabled'
            event_name = '.'.join(['edx', 'certificate', 'generation', cert_event_type])
            self.assert_event_emitted(
                event_name,
                course_id=str(self.COURSE_KEY),
            )

        self._assert_enabled_for_course(self.COURSE_KEY, expect_enabled)

    def test_latest_setting_used(self):
        # Enable the feature
        CertificateGenerationConfiguration.objects.create(enabled=True)

        # Enable for the course
        set_cert_generation_enabled(self.COURSE_KEY, True)
        self._assert_enabled_for_course(self.COURSE_KEY, True)

        # Disable for the course
        set_cert_generation_enabled(self.COURSE_KEY, False)
        self._assert_enabled_for_course(self.COURSE_KEY, False)

    def test_setting_is_course_specific(self):
        # Enable the feature
        CertificateGenerationConfiguration.objects.create(enabled=True)

        # Enable for one course
        set_cert_generation_enabled(self.COURSE_KEY, True)
        self._assert_enabled_for_course(self.COURSE_KEY, True)

        # Should be disabled for another course
        other_course = CourseLocator(org='other', course='other', run='other')
        self._assert_enabled_for_course(other_course, False)

    def _assert_enabled_for_course(self, course_key, expect_enabled):
        """Check that self-generated certificates are enabled or disabled for the course. """
        actual_enabled = cert_generation_enabled(course_key)
        assert expect_enabled == actual_enabled


class GenerateExampleCertificatesTest(ModuleStoreTestCase):
    """Test generation of example certificates. """

    COURSE_KEY = CourseLocator(org='test', course='test', run='test')

    def test_generate_example_certs(self):
        # Generate certificates for the course
        CourseModeFactory.create(course_id=self.COURSE_KEY, mode_slug=CourseMode.HONOR)
        with self._mock_xqueue() as mock_queue:
            generate_example_certificates(self.COURSE_KEY)

        # Verify that the appropriate certs were added to the queue
        self._assert_certs_in_queue(mock_queue, 1)

        # Verify that the certificate status is "started"
        self._assert_cert_status({
            'description': 'honor',
            'status': 'started'
        })

    def test_generate_example_certs_with_verified_mode(self):
        # Create verified and honor modes for the course
        CourseModeFactory.create(course_id=self.COURSE_KEY, mode_slug='honor')
        CourseModeFactory.create(course_id=self.COURSE_KEY, mode_slug='verified')

        # Generate certificates for the course
        with self._mock_xqueue() as mock_queue:
            generate_example_certificates(self.COURSE_KEY)

        # Verify that the appropriate certs were added to the queue
        self._assert_certs_in_queue(mock_queue, 2)

        # Verify that the certificate status is "started"
        self._assert_cert_status(
            {
                'description': 'verified',
                'status': 'started'
            },
            {
                'description': 'honor',
                'status': 'started'
            }
        )

    @contextmanager
    def _mock_xqueue(self):
        """Mock the XQueue method for adding a task to the queue. """
        with patch.object(XQueueCertInterface, 'add_example_cert') as mock_queue:
            yield mock_queue

    def _assert_certs_in_queue(self, mock_queue, expected_num):
        """Check that the certificate generation task was added to the queue. """
        certs_in_queue = [call_args[0] for (call_args, __) in mock_queue.call_args_list]
        assert len(certs_in_queue) == expected_num
        for cert in certs_in_queue:
            assert isinstance(cert, ExampleCertificate)

    def _assert_cert_status(self, *expected_statuses):
        """Check the example certificate status. """
        actual_status = example_certificates_status(self.COURSE_KEY)
        assert list(expected_statuses) == actual_status


@override_settings(FEATURES=FEATURES_WITH_CERTS_ENABLED)
class CertificatesBrandingTest(ModuleStoreTestCase):
    """Test certificates branding. """

    COURSE_KEY = CourseLocator(org='test', course='test', run='test')
    configuration = {
        'logo_image_url': 'test_site/images/header-logo.png',
        'SITE_NAME': 'test_site.localhost',
        'urls': {
            'ABOUT': 'test-site/about',
            'PRIVACY': 'test-site/privacy',
            'TOS_AND_HONOR': 'test-site/tos-and-honor',
        },
    }

    @with_site_configuration(configuration=configuration)
    def test_certificate_header_data(self):
        """
        Test that get_certificate_header_context from lms.djangoapps.certificates api
        returns data customized according to site branding.
        """
        # Generate certificates for the course
        CourseModeFactory.create(course_id=self.COURSE_KEY, mode_slug=CourseMode.HONOR)
        data = get_certificate_header_context(is_secure=True)

        # Make sure there are not unexpected keys in dict returned by 'get_certificate_header_context'
        self.assertCountEqual(
            list(data.keys()),
            ['logo_src', 'logo_url']
        )
        assert self.configuration['logo_image_url'] in data['logo_src']

        assert self.configuration['SITE_NAME'] in data['logo_url']

    @with_site_configuration(configuration=configuration)
    def test_certificate_footer_data(self):
        """
        Test that get_certificate_footer_context from lms.djangoapps.certificates api returns
        data customized according to site branding.
        """
        # Generate certificates for the course
        CourseModeFactory.create(course_id=self.COURSE_KEY, mode_slug=CourseMode.HONOR)
        data = get_certificate_footer_context()

        # Make sure there are not unexpected keys in dict returned by 'get_certificate_footer_context'
        self.assertCountEqual(
            list(data.keys()),
            ['company_about_url', 'company_privacy_url', 'company_tos_url']
        )
        assert self.configuration['urls']['ABOUT'] in data['company_about_url']
        assert self.configuration['urls']['PRIVACY'] in data['company_privacy_url']
        assert self.configuration['urls']['TOS_AND_HONOR'] in data['company_tos_url']


class CertificateAllowlistTests(ModuleStoreTestCase):
    """
    Tests for allowlist functionality.
    """
    def setUp(self):
        super().setUp()

        self.user = UserFactory()
        self.global_staff = GlobalStaffFactory()
        self.course_run = CourseFactory()
        self.course_run_key = self.course_run.id  # pylint: disable=no-member

        CourseEnrollmentFactory(
            user=self.user,
            course_id=self.course_run_key,
            is_active=True,
            mode="verified",
        )

    def test_create_certificate_allowlist_entry(self):
        """
        Test for creating and updating allowlist entries.
        """
        result, __ = create_or_update_certificate_allowlist_entry(self.user, self.course_run_key, "Testing!")

        assert result.course_id == self.course_run_key
        assert result.user == self.user
        assert result.notes == "Testing!"

        result, __ = create_or_update_certificate_allowlist_entry(self.user, self.course_run_key, "New test", False)

        assert result.notes == "New test"
        assert not result.allowlist

    def test_remove_allowlist_entry(self):
        """
        Test for removing an allowlist entry for a user in a given course-run.
        """
        CertificateAllowlistFactory.create(course_id=self.course_run_key, user=self.user)
        assert is_on_allowlist(self.user, self.course_run_key)

        result = remove_allowlist_entry(self.user, self.course_run_key)
        assert result
        assert not is_on_allowlist(self.user, self.course_run_key)

    def test_remove_allowlist_entry_with_certificate(self):
        """
        Test for removing an allowlist entry. Verify that we also invalidate the certificate for the student.
        """
        CertificateAllowlistFactory.create(course_id=self.course_run_key, user=self.user)
        GeneratedCertificateFactory.create(
            user=self.user,
            course_id=self.course_run_key,
            status=CertificateStatuses.downloadable,
            mode='verified'
        )
        assert is_on_allowlist(self.user, self.course_run_key)

        result = remove_allowlist_entry(self.user, self.course_run_key)
        assert result

        certificate = GeneratedCertificate.objects.get(user=self.user, course_id=self.course_run_key)
        assert certificate.status == CertificateStatuses.unavailable
        assert not is_on_allowlist(self.user, self.course_run_key)

    def test_remove_allowlist_entry_entry_dne(self):
        """
        Test for removing an allowlist entry that does not exist
        """
        result = remove_allowlist_entry(self.user, self.course_run_key)
        assert not result

    def test_get_allowlist_entry(self):
        """
        Test to verify that we can retrieve an allowlist entry for a learner.
        """
        allowlist_entry = CertificateAllowlistFactory.create(course_id=self.course_run_key, user=self.user)

        retrieved_entry = get_allowlist_entry(self.user, self.course_run_key)

        assert retrieved_entry.id == allowlist_entry.id
        assert retrieved_entry.course_id == allowlist_entry.course_id
        assert retrieved_entry.user == allowlist_entry.user

    def test_get_allowlist_entry_dne(self):
        """
        Test to verify behavior when an allowlist entry for a user does not exist
        """
        expected_messages = [
            f"Attempting to retrieve an allowlist entry for student {self.user.id} in course {self.course_run_key}.",
            f"No allowlist entry found for student {self.user.id} in course {self.course_run_key}."
        ]

        with LogCapture() as log:
            retrieved_entry = get_allowlist_entry(self.user, self.course_run_key)

        assert retrieved_entry is None

        for index, message in enumerate(expected_messages):
            assert message in log.records[index].getMessage()

    def test_is_on_allowlist(self):
        """
        Test to verify that we return True when an allowlist entry exists.
        """
        CertificateAllowlistFactory.create(course_id=self.course_run_key, user=self.user)

        result = is_on_allowlist(self.user, self.course_run_key)
        assert result

    def test_is_on_allowlist_expect_false(self):
        """
        Test to verify that we will not return False when no allowlist entry exists.
        """
        result = is_on_allowlist(self.user, self.course_run_key)
        assert not result

    def test_is_on_allowlist_entry_disabled(self):
        """
        Test to verify that we will return False when the allowlist entry if it is disabled.
        """
        CertificateAllowlistFactory.create(course_id=self.course_run_key, user=self.user, allowlist=False)

        result = is_on_allowlist(self.user, self.course_run_key)
        assert not result

    def test_can_be_added_to_allowlist(self):
        """
        Test to verify that a learner can be added to the allowlist that fits all needed criteria.
        """
        assert can_be_added_to_allowlist(self.user, self.course_run_key)

    def test_can_be_added_to_allowlist_not_enrolled(self):
        """
        Test to verify that a learner will be rejected from the allowlist without an active enrollment in a
        course-run.
        """
        new_course_run = CourseFactory()

        assert not can_be_added_to_allowlist(self.user, new_course_run.id)  # pylint: disable=no-member

    def test_can_be_added_to_allowlist_certificate_invalidated(self):
        """
        Test to verify that a learner will be rejected from the allowlist if they currently appear on the certificate
        invalidation list.
        """
        certificate = GeneratedCertificateFactory.create(
            user=self.user,
            course_id=self.course_run_key,
            status=CertificateStatuses.unavailable,
            mode='verified'
        )
        CertificateInvalidationFactory.create(
            generated_certificate=certificate,
            invalidated_by=self.global_staff,
            active=True
        )

        assert not can_be_added_to_allowlist(self.user, self.course_run_key)

    def test_can_be_added_to_allowlist_is_already_on_allowlist(self):
        """
        Test to verify that a learner will be rejected from the allowlist if they currently already appear on the
        allowlist.
        """
        CertificateAllowlistFactory.create(course_id=self.course_run_key, user=self.user)

        assert not can_be_added_to_allowlist(self.user, self.course_run_key)

    def test_get_users_allowlist(self):
        """
        Test that allowlisted users are returned correctly
        """
        u1 = UserFactory()
        u2 = UserFactory()
        u3 = UserFactory()
        u4 = UserFactory()

        cr1 = CourseFactory()
        key1 = cr1.id  # pylint: disable=no-member
        cr2 = CourseFactory()
        key2 = cr2.id  # pylint: disable=no-member
        cr3 = CourseFactory()
        key3 = cr3.id  # pylint: disable=no-member

        CourseEnrollmentFactory(
            user=u1,
            course_id=key1,
            is_active=True,
            mode="verified",
        )
        CourseEnrollmentFactory(
            user=u2,
            course_id=key1,
            is_active=True,
            mode="verified",
        )
        CourseEnrollmentFactory(
            user=u3,
            course_id=key1,
            is_active=True,
            mode="verified",
        )
        CourseEnrollmentFactory(
            user=u4,
            course_id=key2,
            is_active=True,
            mode="verified",
        )

        # Add user to the allowlist
        CertificateAllowlistFactory.create(course_id=key1, user=u1)
        # Add user to the allowlist, but set allowlist to false
        CertificateAllowlistFactory.create(course_id=key1, user=u2, allowlist=False)
        # Add user to the allowlist in the other course
        CertificateAllowlistFactory.create(course_id=key2, user=u4)

        users = get_allowlisted_users(key1)
        assert 1 == users.count()
        assert users[0].id == u1.id

        users = get_allowlisted_users(key2)
        assert 1 == users.count()
        assert users[0].id == u4.id

        users = get_allowlisted_users(key3)
        assert 0 == users.count()

    def test_add_and_update(self):
        """
        Test add and update of the allowlist
        """
        u1 = UserFactory()
        notes = 'blah'

        # Check before adding user
        entry = get_allowlist_entry(u1, self.course_run_key)
        assert entry is None

        # Add user
        create_or_update_certificate_allowlist_entry(u1, self.course_run_key, notes)
        entry = get_allowlist_entry(u1, self.course_run_key)
        assert entry.notes == notes

        # Update user
        new_notes = 'really useful info'
        create_or_update_certificate_allowlist_entry(u1, self.course_run_key, new_notes)
        entry = get_allowlist_entry(u1, self.course_run_key)
        assert entry.notes == new_notes

    def test_remove(self):
        """
        Test removal from the allowlist
        """
        u1 = UserFactory()
        notes = 'I had a thought....'

        # Add user
        create_or_update_certificate_allowlist_entry(u1, self.course_run_key, notes)
        entry = get_allowlist_entry(u1, self.course_run_key)
        assert entry.notes == notes

        # Remove user
        remove_allowlist_entry(u1, self.course_run_key)
        entry = get_allowlist_entry(u1, self.course_run_key)
        assert entry is None


class CertificateInvalidationTests(ModuleStoreTestCase):
    """
    Tests for the certificate invalidation functionality.
    """
    def setUp(self):
        super().setUp()

        self.global_staff = GlobalStaffFactory()
        self.user = UserFactory()
        self.course_run = CourseFactory()
        self.course_run_key = self.course_run.id  # pylint: disable=no-member

        CourseEnrollmentFactory(
            user=self.user,
            course_id=self.course_run_key,
            is_active=True,
            mode="verified",
        )

    def test_create_certificate_invalidation_entry(self):
        """
        Test to verify that we can use the functionality defined in the Certificates api.py to create certificate
        invalidation entries. This is functionality the Instructor Dashboard django app relies on.
        """
        certificate = GeneratedCertificateFactory.create(
            user=self.user,
            course_id=self.course_run_key,
            status=CertificateStatuses.unavailable,
            mode='verified'
        )

        result = create_certificate_invalidation_entry(certificate, self.global_staff, "Test!")

        assert result.generated_certificate == certificate
        assert result.active is True
        assert result.notes == "Test!"

    def test_get_certificate_invalidation_entry(self):
        """
        Test to verify that we can retrieve a certificate invalidation entry for a learner.
        """
        certificate = GeneratedCertificateFactory.create(
            user=self.user,
            course_id=self.course_run_key,
            status=CertificateStatuses.unavailable,
            mode='verified'
        )

        invalidation = CertificateInvalidationFactory.create(
            generated_certificate=certificate,
            invalidated_by=self.global_staff,
            active=True
        )

        retrieved_invalidation = get_certificate_invalidation_entry(certificate)

        assert retrieved_invalidation.id == invalidation.id
        assert retrieved_invalidation.generated_certificate == certificate
        assert retrieved_invalidation.active == invalidation.active

    def test_get_certificate_invalidation_entry_dne(self):
        """
        Test to verify behavior when a certificate invalidation entry does not exist.
        """
        certificate = GeneratedCertificateFactory.create(
            user=self.user,
            course_id=self.course_run_key,
            status=CertificateStatuses.unavailable,
            mode='verified'
        )

        expected_messages = [
            f"Attempting to retrieve certificate invalidation entry for certificate with id {certificate.id}.",
            f"No certificate invalidation found linked to certificate with id {certificate.id}.",
        ]

        with LogCapture() as log:
            retrieved_invalidation = get_certificate_invalidation_entry(certificate)

        assert retrieved_invalidation is None

        for index, message in enumerate(expected_messages):
            assert message in log.records[index].getMessage()
