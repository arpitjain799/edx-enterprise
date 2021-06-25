# -*- coding: utf-8 -*-
"""
Client for connecting to Canvas.
"""
import json
import logging
from http import HTTPStatus

import requests
from requests.utils import quote
from six.moves.urllib.parse import quote_plus, urljoin  # pylint: disable=import-error

from django.apps import apps

from integrated_channels.exceptions import ClientError
from integrated_channels.integrated_channel.client import IntegratedChannelApiClient
from integrated_channels.utils import generate_formatted_log, refresh_session_if_expired

LOGGER = logging.getLogger(__name__)


class CanvasAPIClient(IntegratedChannelApiClient):
    """
    Client for connecting to Canvas.

    Required Canvas auth credentials to instantiate a new client object.
        -  canvas_base_url : the base url of the user's Canvas instance.
        -  client_id : the ID associated with a Canvas developer key.
        -  client_secret : the secret key associated with a Canvas developer key.
        -  refresh_token : the refresh token token retrieved by the `oauth/complete`
        endpoint after the user authorizes the use of their Canvas account.

    Order of Operations:
        Before the client can connect with an Enterprise user's Canvas account, the user will need to
        follow these steps
            - Create a developer key with their Canvas account
            - Provide the ECS team with their developer key's client ID and secret.
            - ECS will return a url for the user to visit which will prompt authorization and redirect when
            the user hits the `confirm` button.
            - The redirect will hit the `oauth/complete` endpoint which will use the passed oauth code
            to request the Canvas oauth refresh token and save it to the Enterprise user's Canvas API config
            - The refresh token is used at client instantiation to request the user's access token, this access
            token is saved to the client's session and is used to make POST and DELETE requests to Canvas.
    """

    def __init__(self, enterprise_configuration):
        """
        Instantiate a new client.

        Args:
            enterprise_configuration (CanvasEnterpriseCustomerConfiguration): An enterprise customers's
            configuration model for connecting with Canvas
        """
        super().__init__(enterprise_configuration)
        self.config = apps.get_app_config('canvas')
        self.session = None
        self.expires_at = None
        self.course_create_url = CanvasAPIClient.course_create_endpoint(
            self.enterprise_configuration.canvas_base_url,
            self.enterprise_configuration.canvas_account_id
        )
        self.root_canvas_account = self._find_root_canvas_account()

    @staticmethod
    def course_create_endpoint(canvas_base_url, canvas_account_id):
        """
        Returns endpoint to POST to for course creation
        """
        return '{}/api/v1/accounts/{}/courses'.format(
            canvas_base_url,
            canvas_account_id,
        )

    @staticmethod
    def course_fetch_endpoint(canvas_base_url, canvas_account_id):
        """
        Returns endpoint to fetch all courses for the specified Canvas account
        """
        return '{}/api/v1/accounts/{}/courses'.format(
            canvas_base_url,
            canvas_account_id,
        )

    @staticmethod
    def course_update_endpoint(canvas_base_url, course_id):
        """
        Returns endpoint to PUT to for course update
        """
        return '{}/api/v1/courses/{}'.format(
            canvas_base_url,
            course_id,
        )

    def create_content_metadata(self, serialized_data):
        """
        Creates a course in Canvas.
        If course is not found, easy!  create it as usual
        If course is found, it will have one of the following `workflow_state` values:
                available: issue an update with latest field values
                completed: this happens if a course has been concluded. Update it to change status
                  to offer by using course[event]=offer (which makes course published in Canvas)
                unpublished: still just update
                deleted: take no action for now.
        For information of Canvas workflow_states see `course[event]` at:
        https://canvas.instructure.com/doc/api/courses.html#method.courses.update
        """
        self._create_session()

        course_details = json.loads(serialized_data.decode('utf-8'))['course']
        edx_course_id = course_details['integration_id']
        located_course = self._find_course_by_course_id(edx_course_id)

        if not located_course:
            # Course does not exist: Create the course
            status_code, response_text = self._post(self.course_create_url, serialized_data)
            created_course_id = json.loads(response_text)['id']

            # step 2: upload image_url and any other details
            self._update_course_details(created_course_id, course_details)
        else:
            workflow_state = located_course['workflow_state']
            if workflow_state.lower() == 'deleted':
                LOGGER.error(
                    generate_formatted_log(
                        'canvas',
                        self.enterprise_configuration.enterprise_customer.uuid,
                        None,
                        edx_course_id,
                        'Course with integration_id = {edx_course_id} found in deleted state, '
                        'not attempting to create/update'.format(
                            edx_course_id=edx_course_id,
                        ),
                    )
                )
                status_code = 200
                response_text = 'Course was deleted previously, skipping create/update'
            else:
                # 'unpublished', 'completed' or 'available' cases
                LOGGER.warning(
                    generate_formatted_log(
                        'canvas',
                        self.enterprise_configuration.enterprise_customer.uuid,
                        None,
                        edx_course_id,
                        'Course with canvas_id = {course_id},'
                        'integration_id = {edx_course_id} found in workflow_state={workflow_state},'
                        ' attempting to update instead of creating it'.format(
                            course_id=located_course["id"],
                            edx_course_id=edx_course_id,
                            workflow_state=workflow_state,
                        ),
                    )
                )
                status_code, response_text = self._update_course_details(
                    located_course['id'],
                    course_details,
                )

        return status_code, response_text

    def update_content_metadata(self, serialized_data):
        self._create_session()

        integration_id = self._extract_integration_id(serialized_data)
        course_id = self._get_course_id_from_edx_course_id(integration_id)

        url = CanvasAPIClient.course_update_endpoint(
            self.enterprise_configuration.canvas_base_url,
            course_id,
        )

        return self._put(url, serialized_data)

    def delete_content_metadata(self, serialized_data):
        self._create_session()

        integration_id = self._extract_integration_id(serialized_data)
        course_id = self._get_course_id_from_edx_course_id(integration_id)

        url = '{}/api/v1/courses/{}'.format(
            self.enterprise_configuration.canvas_base_url,
            course_id,
        )

        return self._delete(url)

    def create_assessment_reporting(self, user_id, payload):
        """
        Send assessment level learner data, retrieved by the integrated channels exporter, to Canvas in the form of
        an assignment and submission.
        """
        learner_data = json.loads(payload)
        self._create_session()

        # Retrieve the Canvas user ID from the user's edx email (it is assumed that the learner's Edx
        # and Canvas emails will match).
        canvas_user_id = self._search_for_canvas_user_by_email(user_id)

        canvas_course_id = self._handle_get_user_canvas_course(canvas_user_id, learner_data['courseID'])

        # Depending on if the assignment already exists, either retrieve or create it.
        # Assessment level reporting Canvas assignments use the subsection ID as the primary identifier, whereas
        # course level reporting assignments rely on the course run key.
        assignment_id = self._handle_canvas_assignment_retrieval(
            learner_data['subsectionID'],
            canvas_course_id,
            learner_data['subsection_name'],
            learner_data['points_possible'],
            is_assessment_grade=True
        )

        # The percent grade from the grades api is represented as a decimal, but we can report the percent in the
        # request body as the string: `<int percent grade>%`
        update_grade_response = self._handle_canvas_assignment_submission(
            "{}%".format(str(learner_data['grade'] * 100)),
            canvas_course_id,
            assignment_id,
            canvas_user_id
        )

        return update_grade_response.status_code, update_grade_response.text

    def create_course_completion(self, user_id, payload):  # pylint: disable=unused-argument
        learner_data = json.loads(payload)
        self._create_session()

        # Retrieve the Canvas user ID from the user's edx email (it is assumed that the learner's Edx
        # and Canvas emails will match).
        canvas_user_id = self._search_for_canvas_user_by_email(user_id)

        canvas_course_id = self._handle_get_user_canvas_course(canvas_user_id, learner_data['courseID'])

        # Depending on if the assignment already exists, either retrieve or create it.
        assignment_id = self._handle_canvas_assignment_retrieval(
            learner_data['courseID'],
            canvas_course_id,
            '(Edx integration) Final Grade'
        )

        # Course completion percentage grades are exported as decimals but reported to Canvas as integer percents.
        update_grade_response = self._handle_canvas_assignment_submission(
            learner_data['grade'] * 100,
            canvas_course_id,
            assignment_id,
            canvas_user_id
        )

        return update_grade_response.status_code, update_grade_response.text

    def delete_course_completion(self, user_id, payload):  # pylint: disable=unused-argument
        # Todo: There isn't a great way for users to delete course completion data
        pass

    # Private Methods
    def _update_course_details(self, course_id, course_details):
        """
        Update a course for image_url (and possibly other settings in future).
        Also sets course to 'offer' state by sending 'course[event]=offer',
        which makes the course published in Canvas.

        Arguments:
          - course_id (Number): Canvas Course id
          - course_details (dict): { 'image_url' } : optional, used if present for course[image_url]
        """
        response_code = None
        response_text = None
        url = CanvasAPIClient.course_update_endpoint(
            self.enterprise_configuration.canvas_base_url,
            course_id,
        )
        # Providing the param `event` and setting it to `offer` is equivalent to publishing the course.
        update_payload = {'course': {'event': 'offer'}}
        try:
            # there is no way to do this in a single request during create
            # https://canvas.instructure.com/doc/api/all_resources.html#method.courses.update
            if "image_url" in course_details:
                update_payload['course']['image_url'] = course_details['image_url']

            response_code, response_text = self._put(url, json.dumps(update_payload).encode('utf-8'))
        except Exception as course_exc:  # pylint: disable=broad-except
            # we do not want course image update to cause failures
            edx_course_id = course_details["integration_id"]
            exc_string = str(course_exc)
            LOGGER.error(
                generate_formatted_log(
                    'canvas',
                    self.enterprise_configuration.enterprise_customer.uuid,
                    None,
                    edx_course_id,
                    'Failed to update details for course, '
                    'canvas_course_id={canvas_course_id}. '
                    'Details: {details}'.format(
                        canvas_course_id=course_id,
                        details=exc_string,
                    )
                )
            )

        return response_code, response_text

    def _post(self, url, data):
        """
        Make a POST request using the session object to a Canvas endpoint.

        Args:
            url (str): The url to send a POST request to.
            data (bytearray): The json encoded payload to POST.
        """
        post_response = self.session.post(url, data=data)
        if post_response.status_code >= 400:
            raise ClientError(post_response.text, post_response.status_code)
        return post_response.status_code, post_response.text

    def _put(self, url, data):
        """
        Make a PUT request using the session object to the Canvas course update endpoint

        Args:
            url (str): The canvas url to send update requests to.
            data (bytearray): The json encoded payload to UPDATE. This also contains the integration
            ID used to match a course with a course ID.
        """
        put_response = self.session.put(url, data=data)
        if put_response.status_code >= 400:
            raise ClientError(put_response.text, put_response.status_code)
        return put_response.status_code, put_response.text

    def _delete(self, url):
        """
        Make a DELETE request using the session object to the Canvas course delete endpoint.
        this actually only 'conclude's a course. See this link for difference between
        conclude and delete. Conclude allows bringing course back to 'offer' state
        https://canvas.instructure.com/doc/api/courses.html#method.courses.destroy

        Args:
            url (str): The canvas url to send delete requests to.
        """
        delete_response = self.session.delete(url, data='{"event":"conclude"}')
        if delete_response.status_code >= 400:
            raise ClientError(delete_response.text, delete_response.status_code)
        return delete_response.status_code, delete_response.text

    def _extract_integration_id(self, data):
        """
        Retrieve the integration ID string from the encoded transmission data and apply appropriate
        error handling.

        Args:
            data (bytearray): The json encoded payload intended for a Canvas endpoint.
        """
        if not data:
            raise ClientError("No data to transmit.", HTTPStatus.NOT_FOUND.value)
        try:
            integration_id = json.loads(
                data.decode("utf-8")
            )['course']['integration_id']
        except KeyError as error:
            raise ClientError(
                "Could not transmit data, no integration ID present.", HTTPStatus.NOT_FOUND.value
            ) from error
        except AttributeError as error:
            raise ClientError(
                "Unable to decode data.", HTTPStatus.BAD_REQUEST.value
            ) from error

        return integration_id

    def _find_course_in_account(self, edx_course_id, canvas_account_id):
        """
        Search course by edx_course_id (used as integration_id in canvas) under provided account.
        It will even return courses that are in the 'deleted' state in Canvas, so we can correctly
        skip these courses in logic as needed.

        Note: we do not need to follow pagination here since it would be extremely unlikely
        that searching by a specific edx_course_id results in many records, we generally only
        expect 1 record to come back anyway.

        The `&state[]=all` is added so we can also fetch priorly 'delete'd courses as well
        """
        url = "{}/api/v1/accounts/{}/courses/?search_term={}&state[]=all".format(
            self.enterprise_configuration.canvas_base_url,
            canvas_account_id,
            quote(edx_course_id),
        )
        resp = self.session.get(url)
        all_courses_response = resp.json()

        if resp.status_code >= 400:
            message = ''
            if 'reason' in all_courses_response:
                message = all_courses_response['reason']
            elif 'errors' in all_courses_response:
                message = str(all_courses_response['errors'])
            raise ClientError(
                message,
                all_courses_response.status_code
            )

        course_found = None
        for course in all_courses_response:
            if course['integration_id'] == edx_course_id:
                course_found = course
                break
        return course_found

    def _find_root_canvas_account(self):
        """
        Attempts to find root account id from Canvas.
        If it cannot be found, returns None
        """
        self._create_session()
        url = "{}/api/v1/accounts".format(self.enterprise_configuration.canvas_base_url)
        resp = self.session.get(url)
        all_accounts = resp.json()
        root_account = None
        for account in all_accounts:
            if account['parent_account_id'] is None:
                root_account = account
                break
        return root_account

    def _find_course_by_course_id(self, edx_course_id):
        """
        First attempts to find courase under current account id
        As fallback, to account for cases where course was priorly transmitted to a different
        account, it also searches under the root account for the course.

        Returns:
        - Course dict if the course found in Canvas,
        - None otherwise
        """
        course = self._find_course_in_account(edx_course_id, self.enterprise_configuration.canvas_account_id)
        if not course:
            # now let's try the root account instead (searches under all subaccounts)
            course = self._find_course_in_account(edx_course_id, self.root_canvas_account['id'])
            if course:
                LOGGER.info(generate_formatted_log(
                    'canvas',
                    self.enterprise_configuration.enterprise_customer.uuid,
                    None,
                    edx_course_id,
                    'Found course under root Canvas account'
                ))
        return course

    def _get_course_id_from_edx_course_id(self, edx_course_id):
        """
        Uses the Canvas search api to find a course by edx_course_id

        Args:
            edx_course_id (string): Course ID to search by
        """
        course = self._find_course_by_course_id(edx_course_id)

        if not course:
            raise ClientError(
                "No Canvas courses found with associated edx course ID: {}.".format(
                    edx_course_id
                ),
                HTTPStatus.NOT_FOUND.value
            )
        return course['id']

    def _search_for_canvas_user_by_email(self, user_email):
        """
        Helper method to make an api call to Canvas using the user's email as a search term.

        Args:
            user_email (string) : The email associated with both the user's Edx account and Canvas account.
        """
        get_user_id_from_email_url = '{url_base}/api/v1/accounts/{account_id}/users?search_term={email_address}'.format(
            url_base=self.enterprise_configuration.canvas_base_url,
            account_id=self.enterprise_configuration.canvas_account_id,
            email_address=quote_plus(user_email)  # emails with unique symbols such as `+` can cause issues
        )
        rsps = self.session.get(get_user_id_from_email_url)

        if rsps.status_code >= 400:
            raise ClientError(
                "Failed to retrieve user from Canvas: received response-[{}]".format(rsps.reason),
                rsps.status_code
            )

        get_users_by_email_response = rsps.json()

        try:
            canvas_user_id = get_users_by_email_response[0]['id']
        except (KeyError, IndexError) as error:
            raise ClientError(
                "No Canvas user ID found associated with email: {}".format(user_email),
                HTTPStatus.NOT_FOUND.value
            ) from error
        return canvas_user_id

    def _get_canvas_user_courses_by_id(self, user_id):
        """Helper method to retrieve all courses that a Canvas user is enrolled in."""
        get_users_courses_url = '{canvas_base_url}/api/v1/users/{canvas_user_id}/courses'.format(
            canvas_base_url=self.enterprise_configuration.canvas_base_url,
            canvas_user_id=user_id
        )
        rsps = self.session.get(get_users_courses_url)

        if rsps.status_code >= 400:
            raise ClientError(
                "Could not retrieve Canvas course list. Received exception: {}".format(
                    rsps.reason
                ),
                rsps.status_code
            )

        return rsps.json()

    def _handle_canvas_assignment_retrieval(
            self,
            integration_id,
            course_id,
            assignment_name,
            points_possible=100,
            is_assessment_grade=False
    ):
        """
        Helper method to handle course assignment creation or retrieval. Canvas requires an assignment
        in order for a user to get a grade, so first check the course for the "final grade"
        assignment. This assignment will have a matching integration id to the currently transmitting
        learner data. If this assignment is not yet created on Canvas, send a post request to do so.

        Args:
            integration_id (str) : the string integration id from the edx course.
            course_id (str) : the Canvas course ID relating to the course which the client is currently
            transmitting learner data to.
        """
        # Check if the course assignment already exists
        canvas_assignments_url = '{canvas_base_url}/api/v1/courses/{course_id}/assignments'.format(
            canvas_base_url=self.enterprise_configuration.canvas_base_url,
            course_id=course_id
        )
        resp = self.session.get(canvas_assignments_url)

        more_pages_present = True
        current_page_count = 0
        assignment_id = ''

        # current_page_count serves as a timeout, limiting to a max of 150 pages of requests
        while more_pages_present and current_page_count < 150:
            if resp.status_code >= 400:
                raise ClientError(
                    "Something went wrong retrieving assignments from Canvas. Got response: {}".format(
                        resp.text,
                    ),
                    resp.status_code
                )

            assignments_resp = resp.json()
            for assignment in assignments_resp:
                try:
                    if assignment['integration_id'] == integration_id:
                        assignment_id = assignment['id']
                        break

                # The integration ID check above should ensure that we have a 200 response from Canvas,
                # but sanity catch if we have a unexpected response format
                except (KeyError, ValueError, TypeError) as error:
                    raise ClientError(
                        "Something went wrong retrieving assignments from Canvas. Got response: {}".format(
                            resp.text,
                        ),
                        resp.status_code
                    ) from error

            if not assignment_id:
                # Canvas pagination headers come back as a string ie:
                # 'headers': {
                #     'Link': '<{assignment_url}?page=2&per_page=10>; rel="current",' \
                #             '<{assignment_url}?page=1&per_page=10>; rel="prev",' \
                #             '<{assignment_url}?page=1&per_page=10>; rel="first",' \
                #             '<{assignment_url}?page=2&per_page=10>; rel="last"' \
                # }
                # so we have to parse out the linked list of assignment pages
                assignment_page_results = resp.headers['Link'].split(',')
                pages = {}
                for page in assignment_page_results:
                    page_type = page.split('; rel=')[1].strip('"')
                    pages[page_type] = page.split(';')[0].strip('<>')

                if pages.get('current') == pages.get('last', None):
                    more_pages_present = False
                else:
                    resp = self.session.get(pages.get('next'))
                    current_page_count += 1
            else:
                more_pages_present = False

        # Canvas requires a course assignment for a learner to be assigned a grade.
        # If no assignment has been made yet, create it.
        if not assignment_id:
            assignment_creation_data = {
                'assignment': {
                    'name': assignment_name,
                    'submission_types': 'none',
                    'integration_id': integration_id,
                    'published': True,
                    'points_possible': points_possible,
                    'omit_from_final_grade': is_assessment_grade,
                }
            }
            create_assignment_resp = self.session.post(canvas_assignments_url, json=assignment_creation_data)

            try:
                assignment_id = create_assignment_resp.json()['id']
            except (ValueError, KeyError) as error:
                raise ClientError(
                    "Something went wrong creating an assignment on Canvas. Got response: {}".format(
                        create_assignment_resp.text,
                    ),
                    create_assignment_resp.status_code
                ) from error
        return assignment_id

    def _handle_canvas_assignment_submission(self, grade, course_id, assignment_id, canvas_user_id):
        """
        Helper method to take necessary learner data and post to Canvas as a submission to the correlated assignment.
        """
        submission_url = '{base_url}/api/v1/courses/{course_id}/assignments/' \
            '{assignment_id}/submissions/{user_id}'.format(
                base_url=self.enterprise_configuration.canvas_base_url,
                course_id=course_id,
                assignment_id=assignment_id,
                user_id=canvas_user_id
            )

        # The percent grade from the grades api is represented as a decimal
        submission_data = {
            'submission': {
                'posted_grade': grade
            }
        }
        submission_response = self.session.put(submission_url, json=submission_data)

        if submission_response.status_code >= 400:
            raise ClientError(
                "Something went wrong while posting a submission to Canvas assignment: {} under Canvas course: {}."
                " Recieved response {} with the status code: {}".format(
                    assignment_id,
                    course_id,
                    submission_response.text,
                    submission_response.status_code
                )
            )
        return submission_response

    def _handle_get_user_canvas_course(self, canvas_user_id, learner_data_course_id):
        """
        Helper method to take the Canvas user ID and edX course ID to find the matching Canvas course information.
        """
        # With the Canvas user ID, retrieve all courses for the user.
        user_courses = self._get_canvas_user_courses_by_id(canvas_user_id)

        # Find the course who's integration ID matches the learner data course ID. This integration ID can be either
        # an edX course run ID or course ID. Raise if no course found.
        canvas_course_id = None
        for course in user_courses:
            if course['integration_id'] == learner_data_course_id:
                canvas_course_id = course['id']
                break

        if not canvas_course_id:
            raise ClientError(
                "Course: {course_id} not found registered in Canvas for Canvas learner: {canvas_user_id}.".format(
                    course_id=learner_data_course_id,
                    canvas_user_id=canvas_user_id,
                ),
                HTTPStatus.NOT_FOUND.value,
            )

        return canvas_course_id

    def _create_session(self):
        """
        Instantiate a new session object for use in connecting with Canvas. Each enterprise customer
        connecting to Canvas should have a single client session.
        Will only create a new session if token expiry has been reached
        """
        self.session, self.expires_at = refresh_session_if_expired(
            self._get_oauth_access_token,
            self.session,
            self.expires_at,
        )

    def _get_oauth_access_token(self):
        """Uses the client id, secret and refresh token to request the user's auth token from Canvas.

        Returns:
            access_token (str): the OAuth access token to access the Canvas API as the user
            expires_in (int): the number of seconds after which token will expire
        Raises:
            HTTPError: If we received a failure response code from Canvas.
            ClientError: If an unexpected response format was received that we could not parse.
        """
        client_id = self.enterprise_configuration.client_id
        client_secret = self.enterprise_configuration.client_secret

        if not client_id:
            raise ClientError(
                "Failed to generate oauth access token: Client ID required.",
                HTTPStatus.INTERNAL_SERVER_ERROR
            )
        if not client_secret:
            raise ClientError(
                "Failed to generate oauth access token: Client secret required.",
                HTTPStatus.INTERNAL_SERVER_ERROR
            )
        if not self.enterprise_configuration.refresh_token:
            raise ClientError(
                "Failed to generate oauth access token: Refresh token required.",
                HTTPStatus.INTERNAL_SERVER_ERROR
            )

        if not self.enterprise_configuration.canvas_base_url or not self.config.oauth_token_auth_path:
            raise ClientError(
                "Failed to generate oauth access token: Canvas oauth path missing from configuration.",
                HTTPStatus.INTERNAL_SERVER_ERROR
            )
        auth_token_url = urljoin(
            self.enterprise_configuration.canvas_base_url,
            self.config.oauth_token_auth_path,
        )

        auth_token_params = {
            'grant_type': 'refresh_token',
            'client_id': client_id,
            'client_secret': client_secret,
            'state': str(self.enterprise_configuration.enterprise_customer.uuid),
            'refresh_token': self.enterprise_configuration.refresh_token,
        }

        auth_response = requests.post(auth_token_url, auth_token_params)
        if auth_response.status_code >= 400:
            raise ClientError(auth_response.text, auth_response.status_code)
        try:
            data = auth_response.json()
            return data['access_token'], data["expires_in"]
        except (KeyError, ValueError) as error:
            raise ClientError(auth_response.text, auth_response.status_code) from error
