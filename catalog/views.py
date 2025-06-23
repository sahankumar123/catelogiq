from django.shortcuts import render, redirect
from django.contrib import messages
from django.core.cache import cache
# from .models import UploadedFile
from django.conf import settings
import logging
import json
from databricks.sql import connect
import os
from datetime import datetime, timedelta
from django.http import JsonResponse, HttpResponse
from django.views.decorators.csrf import csrf_exempt
from django.http import FileResponse
from io import BytesIO
from urllib.parse import urlencode
from azure.storage.filedatalake import DataLakeServiceClient
import requests
import pandas as pd
from django.utils import timezone
from django.core.mail import send_mail
from datetime import datetime, timedelta
import inspect
import random
import string
import uuid

from .forms import LoginForm, SignupForm
from catalog.models import PasswordResetToken
from django.contrib.auth.hashers import make_password, check_password
from azure.storage.filedatalake import DataLakeServiceClient
from urllib.parse import urlencode
import requests
import pandas as pd
from django.utils import timezone
import inspect
from django.core.paginator import Paginator

logger = logging.getLogger(__name__)

# Global tracking
file_uploaded = False
selected_file = None

def check_file_uploaded():
    """Check if a file has been uploaded in the current session."""
    logger.debug(f"File uploaded status: {file_uploaded}")
    return file_uploaded

def get_user_by_email(email):
    try:
        with connect(
            server_hostname=settings.DATABRICKS_HOST,
            http_path=settings.DATABRICKS_HTTP_PATH,
            access_token=settings.DATABRICKS_TOKEN
        ) as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT username, email, phone_number, password FROM text_log_analytics_catalog.login_credentials.login WHERE email = ?",
                (email,))
            return cursor.fetchone()
    except Exception as e:
        logger.error(f"Databricks error: {e}")
        return None


def home(request):
    user_name = request.session.get('user_name')
    return render(request, 'home.html', {'user': {'name': user_name} if user_name else None})


def login_view(request):
    if request.method == 'POST':
        form = LoginForm(request.POST)
        if form.is_valid():
            email = form.cleaned_data['email']
            password = form.cleaned_data['password']
            user = get_user_by_email(email)
            if user and check_password(password, user[3]):
                request.session['user_id'] = user[0]
                request.session['user_name'] = user[1]
                return redirect('/')
            else:
                form.add_error(None, 'Invalid email or password')
    else:
        form = LoginForm()
    return render(request, 'login.html', {'form': form})


def signup_view(request):
    if request.session.get('user_id'):
        return redirect('home')

    if request.method == 'POST':
        form = SignupForm(request.POST)
        if form.is_valid():
            otp = ''.join(random.choices(string.digits, k=6))
            request.session['signup_data'] = {
                'name': form.cleaned_data['name'],
                'email': form.cleaned_data['email'],
                'phone_number': form.cleaned_data['phone_number'],
                'password': form.cleaned_data['password']
            }
            request.session['otp'] = otp
            request.session.modified = True

            try:
                send_mail(
                    subject='CatelogIQ Signup OTP',
                    message=f'Your OTP for signup is: {otp}',
                    from_email=settings.EMAIL_HOST_USER,
                    recipient_list=[form.cleaned_data['email']],
                    fail_silently=False,
                )
                messages.info(request, 'An OTP has been sent to your email.')
                return redirect('verify_otp')
            except Exception as e:
                logger.error(f"Failed to send OTP email: {e}")
                messages.error(request, 'Failed to send OTP. Please try again.')
    else:
        form = SignupForm()
    return render(request, 'signup.html', {'form': form})


def verify_otp(request):
    if request.method == 'POST':
        entered_otp = request.POST.get('otp', '').strip()
        stored_otp = request.session.get('otp')
        signup_data = request.session.get('signup_data')

        if not signup_data or not stored_otp:
            messages.error(request, 'Session expired or invalid. Please try signing up again.')
            return redirect('signup')

        if entered_otp == stored_otp:
            try:
                with connect(
                    server_hostname=settings.DATABRICKS_HOST,
                    http_path=settings.DATABRICKS_HTTP_PATH,
                    access_token=settings.DATABRICKS_TOKEN
                ) as conn:
                    cursor = conn.cursor()
                    cursor.execute("""
                        INSERT INTO text_log_analytics_catalog.login_credentials.login 
                        (username, email, phone_number, password)
                        SELECT ?, ?, ?, ?
                    """, (
                        signup_data['name'],
                        signup_data['email'],
                        signup_data['phone_number'],
                        make_password(signup_data['password'])
                    ))
                    conn.commit()

                del request.session['otp']
                del request.session['signup_data']
                request.session.modified = True
                messages.success(request, 'Account created successfully! Please log in.')
                return redirect('login')
            except Exception as e:
                logger.error(f"Failed to save user: {e}")
                messages.error(request, 'Error creating account. Please try again.')
        else:
            messages.error(request, 'Invalid OTP. Please try again.')
    return render(request, 'verify_otp.html', {'email': request.session.get('signup_data', {}).get('email', '')})


def forgot_password(request):
    if request.method == 'POST':
        email = request.POST.get('email', '').strip()
        user = get_user_by_email(email)
        if user:
            try:
                token = str(uuid.uuid4())
                expires_at = timezone.now() + timedelta(minutes=30)
                PasswordResetToken.objects.create(email=email, token=token, expires_at=expires_at)
                reset_link = f"{settings.BASE_URL}/reset-password/{token}/"
                send_mail(
                    subject='CatelogIQ Password Reset',
                    message=f'Click to reset your password: {reset_link}',
                    from_email=settings.EMAIL_HOST_USER,
                    recipient_list=[email],
                    fail_silently=False,
                )
                messages.info(request, 'A password reset link has been sent to your email.')
                return redirect('login')
            except Exception as e:
                logger.error(f"Error during password reset: {e}")
                messages.error(request, 'Error sending reset link. Try again.')
        else:
            messages.error(request, 'No account found with this email.')
    return render(request, 'forgot_password.html')

def reset_password(request, token):
    try:
        reset_token = PasswordResetToken.objects.get(token=token)
        if not reset_token.is_valid():
            messages.error(request, 'Reset link expired.')
            reset_token.delete()
            return redirect('forgot_password')
    except PasswordResetToken.DoesNotExist:
        messages.error(request, 'Invalid reset link.')
        return redirect('forgot_password')

    if request.method == 'POST':
        password = request.POST.get('password')
        confirm_password = request.POST.get('confirm_password')
        if password != confirm_password:
            messages.error(request, 'Passwords do not match.')
        elif len(password) < 6:
            messages.error(request, 'Password must be at least 6 characters long.')
        else:
            try:
                with connect(
                    server_hostname=settings.DATABRICKS_HOST,
                    http_path=settings.DATABRICKS_HTTP_PATH,
                    access_token=settings.DATABRICKS_TOKEN
                ) as conn:
                    cursor = conn.cursor()
                    cursor.execute(
                        "UPDATE text_log_analytics_catalog.login_credentials.login SET password = ? WHERE email = ?",
                        (make_password(password), reset_token.email)
                    )
                    conn.commit()
                reset_token.delete()
                messages.success(request, 'Password reset successfully! Please log in.')
                return redirect('login')
            except Exception as e:
                logger.error(f"Failed to update password: {e}")
                messages.error(request, 'Failed to reset password. Try again.')
    return render(request, 'reset_password.html', {'token': token})

def logout_view(request):
    request.session.flush()
    messages.success(request, "You have been logged out.")
    return redirect('home')


@csrf_exempt
def log_analytics(request):
    global file_uploaded, selected_file
    account_name = getattr(settings, 'AZURE_ACCOUNT_NAME', None)
    account_key = getattr(settings, 'AZURE_ACCOUNT_KEY', None)
    file_system = "bronze"
    directory = "raw_log_files"
    new_file_uploaded = False
    pipeline_completed = request.session.get('pipeline_completed', False)
    logger.debug(f"[log_analytics] Session: pipeline_completed={pipeline_completed}, update_id={request.session.get('pipeline_update_id')}")

    # Validate settings
    if not account_name or not account_key:
        logger.error("Azure account name or key not configured in settings.")
        messages.error(request, "Storage configuration error. Please contact the administrator.")
        return render(request, 'log_analytics.html', {'files': [], 'pipeline_completed': False, 'new_file_uploaded': new_file_uploaded})
 
    # Initialize ADLS Gen2 client
    try:
        service_client = DataLakeServiceClient(
            account_url=f"https://{account_name}.dfs.core.windows.net",
            credential=account_key
        )
        file_system_client = service_client.get_file_system_client(file_system)
        try:
            directory_client = file_system_client.get_directory_client(directory)
            directory_client.get_directory_properties()
        except Exception as e:
            logger.warning(f"Directory {directory} does not exist, creating it: {str(e)}")
            directory_client = file_system_client.create_directory(directory)
    except Exception as e:
        logger.error(f"ADLS Gen2 connection failed: {str(e)}")
        messages.error(request, f"Storage connection failed: {str(e)}")
        return render(request, 'log_analytics.html', {'files': [], 'pipeline_completed': False, 'new_file_uploaded': new_file_uploaded})

    # Get list of files from ADLS Gen2
    file_list = []
    try:
        paths = file_system_client.get_paths(path=directory)
        for path in paths:
            if path.is_directory:
                continue
            file_list.append(path.name.split('/')[-1])
        logger.debug(f"Retrieved {len(file_list)} files from ADLS: {file_list}")
    except Exception as e:
        logger.error(f"Failed to list files in ADLS: {str(e)}")
        messages.error(request, f"Failed to list files: {str(e)}")
        return render(request, 'log_analytics.html', {'files': [], 'selected_file': selected_file, 'pipeline_completed': False, 'new_file_uploaded': new_file_uploaded})
 
    # Function to insert file metadata into Databricks table
    def insert_file_to_databricks(file_name):
        try:
            with connect(
                server_hostname=settings.DATABRICKS_HOST,
                http_path=settings.DATABRICKS_HTTP_PATH,
                access_token=settings.DATABRICKS_TOKEN
            ) as conn:
                cursor = conn.cursor()
                query = """
                    INSERT INTO text_log_analytics_catalog.bronze_schema.uploaded_files (file_name, uploaded_timestamp)
                    VALUES (?, ?)
                """
                cursor.execute(query, (file_name, timezone.now()))
                conn.commit()
                logger.debug(f"Inserted file {file_name} into Databricks table")
        except Exception as e:
            logger.error(f"Failed to insert file {file_name} into Databricks table: {str(e)}")
            messages.error(request, f"Failed to save file metadata to database: {str(e)}")
 
    if request.method == 'POST':
        if 'show_analysis' in request.POST:
            logger.debug(f"[show_analysis] file_uploaded={check_file_uploaded()}, pipeline_completed={pipeline_completed}")
            try:
                if check_file_uploaded() and pipeline_completed:
                    logger.debug("Redirecting to analysis_options")
                    return redirect('analysis_options')
                else:
                    if not check_file_uploaded():
                        messages.error(request, "Please upload a file first.")
                    elif not pipeline_completed:
                        messages.info(request, "Data processing pipeline is still running. Please wait.")
                    return render(request, 'log_analytics.html', {'files': file_list, 'selected_file': selected_file, 'pipeline_completed': pipeline_completed, 'new_file_uploaded': new_file_uploaded})
            except Exception as e:
                logger.error(f"Error processing show analysis: {str(e)}")
                messages.error(request, f"Error processing request: {str(e)}")
                return render(request, 'log_analytics.html', {'files': file_list, 'selected_file': selected_file, 'pipeline_completed': pipeline_completed, 'new_file_uploaded': new_file_uploaded})

        if 'show_analysis_file' in request.POST:
            try:
                selected_file_name = request.POST.get('file_name')
                if selected_file_name:
                    selected_file = selected_file_name
                    file_uploaded = True
                    request.session['pipeline_completed'] = False
                    request.session['pipeline_update_id'] = None
                    request.session.modified = True
                    logger.debug(f"Selected file: {selected_file_name}, file_uploaded=True")
                    insert_file_to_databricks(selected_file_name)
                    return redirect('analysis_options')
                else:
                    messages.error(request, "No file selected for analysis.")
                    return render(request, 'log_analytics.html', {'files': file_list, 'selected_file': selected_file, 'pipeline_completed': pipeline_completed, 'new_file_uploaded': new_file_uploaded})
            except Exception as e:
                logger.error(f"Error processing show analysis file: {str(e)}")
                messages.error(request, f"Error processing file selection: {str(e)}")
                return render(request, 'log_analytics.html', {'files': file_list, 'selected_file': selected_file, 'pipeline_completed': pipeline_completed, 'new_file_uploaded': new_file_uploaded})

        uploaded_file = request.FILES.get('csv_file')
        if not uploaded_file:
            messages.error(request, "No file uploaded.")
            return render(request, 'log_analytics.html', {'files': file_list, 'selected_file': selected_file, 'pipeline_completed': pipeline_completed, 'new_file_uploaded': new_file_uploaded})

        valid_extensions = ('.csv', '.log', '.txt')
        if not uploaded_file.name.lower().endswith(valid_extensions):
            messages.error(request, "Only CSV, log, or text files are allowed.")
            return render(request, 'log_analytics.html', {'files': file_list, 'selected_file': selected_file, 'pipeline_completed': pipeline_completed, 'new_file_uploaded': new_file_uploaded})

        file_exists = False
        try:
            existing_file_client = directory_client.get_file_client(uploaded_file.name)
            existing_file_client.get_file_properties()
            file_exists = True
        except:
            file_exists = False

        if file_exists:
            messages.info(request, f"File '{uploaded_file.name}' already uploaded.")
            try:
                uploaded_file_obj, created = UploadedFile.objects.get_or_create(filename=uploaded_file.name)
                uploaded_file_obj.created_at = timezone.now()
                uploaded_file_obj.analyzed = False
                uploaded_file_obj.output_path = None
                uploaded_file_obj.save()
            except Exception as e:
                logger.error(f"Failed to save metadata for existing file: {str(e)}")

            file_uploaded = True
            selected_file = uploaded_file.name
            request.session['pipeline_completed'] = False
            request.session['pipeline_update_id'] = None
            request.session.modified = True
            insert_file_to_databricks(uploaded_file.name)
            return render(request, 'log_analytics.html', {'files': file_list, 'selected_file': selected_file, 'pipeline_completed': False, 'new_file_uploaded': new_file_uploaded})

        # Save file locally
        temp_path = os.path.join(settings.TEMP_DIR, uploaded_file.name)
        os.makedirs(os.path.dirname(temp_path), exist_ok=True)
        try:
            with open(temp_path, 'wb') as f:
                for chunk in uploaded_file.chunks():
                    f.write(chunk)
        except Exception as e:
            logger.error(f"Failed to save temp file: {str(e)}")
            messages.error(request, f"Temp file save failed: {str(e)}")
            return render(request, 'log_analytics.html', {'files': file_list, 'selected_file': selected_file, 'pipeline_completed': pipeline_completed, 'new_file_uploaded': new_file_uploaded})

        # Upload new file to ADLS Gen2
        try:
            file_client = directory_client.create_file(uploaded_file.name)
            with open(temp_path, 'rb') as data:
                file_contents = data.read()
                file_client.append_data(data=file_contents, offset=0, length=len(file_contents))
                file_client.flush_data(len(file_contents))
        except Exception as e:
            logger.error(f"Upload to ADLS failed: {str(e)}")
            messages.error(request, f"Upload failed: {str(e)}")
            return render(request, 'log_analytics.html', {'files': file_list, 'selected_file': selected_file, 'pipeline_completed': pipeline_completed, 'new_file_uploaded': new_file_uploaded})
 
        # Insert into Databricks table
        insert_file_to_databricks(uploaded_file.name)
 
        # Update model
        try:
            uploaded_file_obj, created = UploadedFile.objects.get_or_create(filename=uploaded_file.name)
            uploaded_file_obj.created_at = timezone.now()
            uploaded_file_obj.analyzed = False
            uploaded_file_obj.output_path = None
            uploaded_file_obj.save()
        except Exception as e:
            logger.error(f"Failed to save metadata: {str(e)}")

        file_uploaded = True
        selected_file = uploaded_file.name
        new_file_uploaded = True
        request.session['pipeline_completed'] = False
        request.session['pipeline_update_id'] = None
        request.session.modified = True
        logger.debug(f"Set file_uploaded=True after uploading {uploaded_file.name}")

        # Trigger DLT pipeline
        try:
            pipeline_id = "d8441aad-ac3a-4e9a-871c-44b4a3f786a7"
            endpoint_url = f"{settings.DATABRICKS_HOST}/api/2.0/pipelines/{pipeline_id}/updates"
            headers = {
                "Authorization": f"Bearer {settings.DATABRICKS_TOKEN}",
                "Content-Type": "application/json"
            }
            payload = {}
            response = requests.post(endpoint_url, headers=headers, json=payload, timeout=30)
            logger.debug(f"DLT trigger response status: {response.status_code}, body: {response.text}")
            if response.status_code == 200:
                update_id = response.json().get('update_id')
                if update_id:
                    request.session['pipeline_update_id'] = update_id
                    request.session.modified = True
                    logger.debug(f"DLT pipeline triggered with update_id: {update_id}")
                    messages.info(request, "File uploaded successfully. Processing pipeline started. Please wait.")
                else:
                    logger.error("No update_id returned")
                    messages.error(request, "Failed to retrieve pipeline run ID.")
            else:
                logger.error(f"Failed to trigger DLT: {response.text}")
                messages.error(request, "Failed to trigger pipeline.")
        except Exception as e:
            logger.error(f"Error triggering DLT: {str(e)}", exc_info=True)
            messages.error(request, "Error triggering pipeline.")

        # Clean up
        if os.path.exists(temp_path):
            os.remove(temp_path)

        messages.success(request, f"File '{uploaded_file.name}' uploaded successfully!")
        return render(request, 'log_analytics.html', {'files': file_list, 'selected_file': selected_file, 'pipeline_completed': False, 'new_file_uploaded': new_file_uploaded})

    # Default return for GET requests
    logger.debug("Rendering log_analytics.html for GET request")
    return render(request, 'log_analytics.html', {'files': file_list, 'selected_file': selected_file, 'pipeline_completed': pipeline_completed, 'new_file_uploaded': new_file_uploaded})

@csrf_exempt
def check_pipeline_status(request):
    if request.method == 'GET':
        update_id = request.session.get('pipeline_update_id')
        logger.debug(f"[check_pipeline_status] update_id={update_id}")
        if not update_id:
            return JsonResponse({'completed': False, 'status': 'No pipeline running'})

        try:
            pipeline_id = "d8441aad-ac3a-4e9a-871c-44b4a3f786a7"
            endpoint_url = f"{settings.DATABRICKS_HOST}/api/2.0/pipelines/{pipeline_id}/updates/{update_id}"
            headers = {
                "Authorization": f"Bearer {settings.DATABRICKS_TOKEN}",
                "Content-Type": "application/json"
            }
            response = requests.get(endpoint_url, headers=headers, timeout=10)
            logger.debug(f"Pipeline status check response status: {response.status_code}, body: {response.text}")

            if response.status_code == 200:
                json_response = response.json()
                status = json_response.get('update', {}).get('state', 'UNKNOWN')
                logger.debug(f"Pipeline status: {status}")
                if status in ['COMPLETED', 'SUCCESS']:
                    request.session['pipeline_completed'] = True
                    request.session.modified = True
                    logger.debug("Set pipeline_completed=True")
                    return JsonResponse({'completed': True, 'status': 'Pipeline completed successfully'})
                elif status in ['FAILED', 'CANCELED']:
                    request.session['pipeline_completed'] = False
                    request.session.modified = True
                    return JsonResponse({'completed': False, 'status': 'Pipeline failed or canceled'})
                else:
                    return JsonResponse({'completed': False, 'status': 'Pipeline still running'})
            else:
                logger.error(f"Failed to check pipeline status: {response.text}")
                return JsonResponse({'completed': False, 'status': 'Error checking pipeline status'})
        except Exception as e:
            logger.error(f"Error checking pipeline status: {str(e)}", exc_info=True)
            return JsonResponse({'completed': False, 'status': 'Error checking pipeline status'})

    return JsonResponse({'error': 'Invalid request method'}, status=400)

@csrf_exempt
def reset_pipeline_status(request):
    if request.method == 'POST':
        try:
            request.session['pipeline_completed'] = False
            request.session['pipeline_update_id'] = None
            request.session.modified = True
            logger.debug("Pipeline status reset: pipeline_completed=False, pipeline_update_id=None")
            return JsonResponse({'status': 'Pipeline status reset'})
        except Exception as e:
            logger.error(f"Error resetting pipeline status: {str(e)}")
            return JsonResponse({'error': 'Failed to reset pipeline status'}, status=500)
    return JsonResponse({'error': 'Invalid request method'}, status=400)

def analysis_options(request):
    logger.debug("Rendering analysis_options.html")
    file_uploaded_status = check_file_uploaded()
    logger.debug(f"File uploaded status in analysis_options: {file_uploaded_status}")
    return render(request, 'analysis_options.html', {'file_uploaded': file_uploaded_status, 'selected_file': selected_file})

def anomaly_detection(request):
    logger.debug("Handling anomaly_detection request")
    if not check_file_uploaded():
        messages.error(request, "Please upload a file first.")
        return redirect('log_analytics')

    superset_iframe_url = (
        "http://20.81.241.192:8088/superset/dashboard/b9341dc2-1385-444f-b612-7a5d69119160/?permalink_key=71mJzoVE80P&standalone=true"
    )

    params = request.GET.dict()
    if params:
        superset_iframe_url += "&" + urlencode(params)

    logger.debug(f"Rendering Superset iframe with URL: {superset_iframe_url}")
    
    try:
        return render(request, 'anomaly_detection.html', {'iframe_url': superset_iframe_url, 'selected_file': selected_file})
    except Exception as e:
        logger.error(f"Error rendering dashboard: {str(e)}")
        messages.error(request, "Failed to load the dashboard. Please try again later.")
        return redirect('log_analytics')

def stream_viewer(request):
    logger.debug("Handling stream_viewer request")
    if not check_file_uploaded():
        messages.error(request, "Please upload a file first.")
        return redirect('log_analytics')
 
    table_name = "text_log_analytics_catalog.silver_schema.processed_logs"
    page_size = 20
    page = request.GET.get('page', '1')
    try:
        page = int(page) if page.isdigit() else 1
        if page < 1:
            page = 1
    except ValueError:
        page = 1

    try:
        # Connect to Databricks SQL warehouse
        with connect(
            server_hostname=settings.DATABRICKS_HOST,
            http_path=settings.DATABRICKS_HTTP_PATH,
            access_token=settings.DATABRICKS_TOKEN
        ) as conn:
            cursor = conn.cursor()
            
            # Get column names
            cursor.execute(f"DESCRIBE TABLE {table_name}")
            columns = [row[0] for row in cursor.fetchall()]
            logger.debug(f"Available columns: {columns}")

            if not columns:
                logger.error(f"No columns found in table {table_name}")
                messages.error(request, "No columns found in data.")
                return render(request, 'stream_viewer.html', {
                    'results': [],
                    'columns': [],
                    'filter_values': {'level': []},
                    'pagination': {},
                    'current_filters': {},
                    'selected_file': selected_file
                })

            # Get distinct level values for filtering
            level_column = 'level' if 'level' in columns else 'log_level' if 'log_level' in columns else None
            cache_key_levels = f"stream_viewer_levels_{table_name}"
            level_values = cache.get(cache_key_levels)
            if not level_values and level_column:
                cursor.execute(f"SELECT DISTINCT {level_column} FROM {table_name} WHERE {level_column} IS NOT NULL LIMIT 100")
                level_values = sorted([row[0] for row in cursor.fetchall() if row[0] is not None])
                cache.set(cache_key_levels, level_values, timeout=3600)
                logger.debug(f"Cached level values: {level_values}")
            else:
                level_values = []

            # Build the base query
            query = f"SELECT * FROM {table_name}"
            conditions = []
            params = []
            current_filters = {}

            # Apply filters
            level_filter = request.GET.get("level")
            if level_filter and level_column and level_filter in level_values:
                conditions.append(f"{level_column} = ?")
                params.append(level_filter)
                current_filters['level'] = level_filter
                logger.debug(f"Applied level filter: {level_filter}")

            tag_filter = request.GET.get("tag")
            if tag_filter and 'tag' in columns:
                conditions.append("tag LIKE ?")
                params.append(f"%{tag_filter}%")
                current_filters['tag'] = tag_filter
                logger.debug(f"Applied tag filter: {tag_filter}")

            message_filter = request.GET.get("message")
            if message_filter and 'message' in columns:
                conditions.append("message LIKE ?")
                params.append(f"%{message_filter}%")
                current_filters['message'] = message_filter
                logger.debug(f"Applied message filter: {message_filter}")

            if conditions:
                query += " WHERE " + " AND ".join(conditions)

            # Execute query with limit
            query += " LIMIT 1000"
            cursor.execute(query, params)
            rows = cursor.fetchall()
            data_list = [dict(zip(columns, row)) for row in rows]
            logger.debug(f"Collected {len(data_list)} rows from table {table_name}")

            # Convert to pandas DataFrame
            pandas_df = pd.DataFrame(data_list)
            if pandas_df.empty:
                logger.warning(f"No data collected from table {table_name}")
                messages.info(request, "No data available to display.")
                return render(request, 'stream_viewer.html', {
                    'results': [],
                    'columns': columns,
                    'filter_values': {'level': level_values},
                    'pagination': {},
                    'current_filters': {},
                    'selected_file': selected_file
                })

            # Pagination
            total_records = len(pandas_df)
            paginator = Paginator(pandas_df.to_dict('records'), page_size)
            try:
                page_obj = paginator.page(page)
            except:
                page_obj = paginator.page(1)
                page = 1
            results = page_obj.object_list
            logger.debug(f"Paginated results: {len(results)} records for page {page}")

            total_pages = paginator.num_pages
            pagination = {
                'current_page': page_obj.number,
                'has_previous': page_obj.has_previous(),
                'has_next': page_obj.has_next(),
                'previous_page': page_obj.previous_page_number() if page_obj.has_previous() else None,
                'next_page': page_obj.next_page_number() if page_obj.has_next() else None,
                'total_pages': total_pages,
                'total_records': total_records
            }

            filter_values = {
                'level': level_values,
                'tag': [],
                'message': []
            }

            if not results and any(current_filters.values()):
                messages.info(request, "No records match the selected filters.")
                logger.debug("No records match the applied filters")

            logger.debug(f"Rendering stream_viewer with {len(results)} records, page {page}")
            return render(request, 'stream_viewer.html', {
                'results': results,
                'columns': columns,
                'filter_values': filter_values,
                'pagination': pagination,
                'current_filters': current_filters,
                'selected_file': selected_file
            })

    except Exception as e:
        logger.error(f"Failed to query table {table_name}: {str(e)}", exc_info=True)
        messages.error(request, f"Failed to load stream data: {str(e)}")
        return render(request, 'stream_viewer.html', {
            'results': [],
            'columns': [],
            'filter_values': {'level': []},
            'pagination': {},
            'current_filters': {},
            'selected_file': selected_file
        })

@csrf_exempt
def chatbot(request):
    if request.method == 'POST':
        try:
            data = json.loads(request.body)
            query = data.get('query', '')
            logger.debug(f"Received query: {query}")

            if not query:
                return JsonResponse({'response': 'Please provide a query.'}, status=400)

            logger.debug(f"Databricks Token: {settings.DATABRICKS_TOKEN}")
            endpoint_url = "https://adb-3623933893880845.5.azuredatabricks.net/serving-endpoints/LogIQ/invocations"
            headers = {
                "Authorization": f"Bearer {settings.DATABRICKS_TOKEN}",
                "Content-Type": "application/json"
            }
            logger.debug(f"Request Headers: {headers}")

            payload = {
                "dataframe_split": {
                    "columns": ["input", "tool_name"],
                    "data": [[query, "android_log"]]
                }
            }
            logger.debug(f"Request Payload: {payload}")

            response = requests.post(endpoint_url, headers=headers, json=payload, timeout=230)
            logger.debug(f"Databricks Response Status: {response.status_code}")
            logger.debug(f"Databricks Response: {response.text}")

            if response.status_code == 200:
                result = response.json()
                predictions = result.get('predictions', {}).get('predictions', [])
                if predictions and isinstance(predictions, list) and len(predictions) > 0:
                    bot_response = predictions[0].get('output', 'Sorry, I could not process that.')
                    status = predictions[0].get('status', 'unknown')
                    if status != 'success':
                        logger.warning(f"Prediction status: {status}")
                        bot_response = f"Analysis failed: {bot_response}"
                else:
                    bot_response = 'Sorry, I could not process that.'
            else:
                logger.error(f"Databricks endpoint error: {response.text}")
                bot_response = f"Error from service: {response.json().get('message', 'Unknown error')}"

            return JsonResponse({'response': bot_response})
        except Exception as e:
            logger.error(f"Chatbot error: {str(e)}", exc_info=True)
            return JsonResponse({'response': f"Error: {str(e)}"}, status=500)
    else:
        if not check_file_uploaded():
            messages.error(request, "Please upload a file first.")
            return redirect('log_analytics')

    return render(request, 'chatbot.html', {'selected_file': selected_file})

NGROK_BASE = "https://5b65-223-184-64-14.ngrok-free.app/superset/dashboard/2751f42b-c289-4698-bde5-1c3994da9b1f/?standalone=true"

def superset_proxy(request, path):
    url = f"{NGROK_BASE}/{path}"
    headers = {
        "ngrok-skip-browser-warning": "1",
        **{k: v for k, v in request.headers.items() if k not in ("Host",)},
    }
    resp = requests.get(url, headers=headers, params=request.GET, stream=True)
    django_resp = HttpResponse(
        resp.raw,
        status=resp.status_code,
        content_type=resp.headers.get("Content-Type", "text/html"),
    )
    return django_resp

def databricks_dashboard_proxy(request):
    if not check_file_uploaded():
        messages.error(request, "Please upload a file first.")
        return redirect('log_analytics')

    superset_iframe_url = (
        "http://20.81.241.192:8088/superset/dashboard/dc794cbe-d35e-4cb8-950b-2e65dda38e9d/?permalink_key=kgLVNR8Jqba&standalone=true"
    )

    params = request.GET.dict()
    if params:
        superset_iframe_url += "&" + urlencode(params)

    logger.debug(f"Rendering Superset iframe with URL: {superset_iframe_url}")

    try:
        return render(request, 'dashboard.html', {'iframe_url': superset_iframe_url, 'selected_file': selected_file})
    except Exception as e:
        logger.error(f"Error rendering dashboard: {str(e)}")
        messages.error(request, "Failed to load the dashboard. Please try again later.")
        return redirect('log_analytics')

def about_us(request):
    return render(request, 'about_us.html', {'selected_file': selected_file})

def contact(request):
    if request.method == 'POST':
        name = request.POST.get('name')
        email = request.POST.get('email')
        message = request.POST.get('message')
        messages.success(request, "Thank you for your message! We'll get back to you soon.")
        return redirect('contact')
    return render(request, 'contact.html', {'selected_file': selected_file})

@csrf_exempt
def text_classification(request):
    if request.method == 'GET':
        return render(request, 'text_classification.html')  # <-- load the form page
    elif request.method == 'POST':
        try:
            data = json.loads(request.body)
            text = data.get('text', '')
            subject = data.get('subject', '')
            message_type = data.get('message_type', 'whatsapp').lower()

            # Prepare dataframe for serving endpoint
            df = pd.DataFrame([{
                'source_type': message_type,
                'subject': subject if message_type == 'email' else None,
                'body': text if message_type == 'email' else None,
                'message': text if message_type != 'email' else None
            }])

            # Call the Databricks serving endpoint
            url = "https://adb-3623933893880845.5.azuredatabricks.net/serving-endpoints/Text_Classification_Message_Classifier/invocations"
            headers = {
                "Authorization": f"Bearer {settings.DATABRICKS_TOKEN}",
                "Content-Type": "application/json"
            }
            payload = {
                "dataframe_split": df.to_dict(orient='split')
            }
            response = requests.post(url, headers=headers, data=json.dumps(payload, allow_nan=True))

            # Check response
            if response.status_code == 200:
                result = response.json()
                predictions = result.get('predictions', [])

                classification_result = "No classification found."
                urgency_result = "No urgency found."

                if predictions and isinstance(predictions, list) and len(predictions) > 0:
                    first = predictions[0]
                    source_type_out = first.get('source_type', 'N/A')
                    queue = first.get('department', 'N/A')
                    urgency = first.get('urgency', 'N/A')
                    input_data = first.get('input', {})
                    display_message = input_data.get('message') or input_data.get('body') or text
                    if source_type_out.lower() == 'email' and subject:
                        classification_result = (
                            f"Source Type = {source_type_out}\n"
                            f"Subject = {subject}\n"
                            f"Message = {display_message}\n"
                            f"Classified as = \"{queue}\""
                        )
                    else:
                        classification_result = (
                            f"Source Type = {source_type_out}\n"
                            f"Message = {display_message}\n"
                            f"Classified as = \"{queue}\""
                        )

                    urgency_result = f"Urgency Level = \"{urgency}\""

                return JsonResponse({
                    'classification': classification_result,
                    'urgency': urgency_result
                })
            else:
                return JsonResponse({'error': f"Databricks request failed: {response.status_code}, {response.text}"}, status=500)

        except Exception as e:
            return JsonResponse({'error': str(e)}, status=500)

    return JsonResponse({'error': 'Invalid request method'}, status=405)

def debug_session(request):
    return JsonResponse({
        'pipeline_completed': request.session.get('pipeline_completed'),
        'pipeline_update_id': request.session.get('pipeline_update_id')
    })

def contact(request):
    if request.method == 'POST':
        name = request.POST.get('name')
        email = request.POST.get('email')
        message = request.POST.get('message')

        # Send email
        subject = f'Contact Form Submission from {name}'
        message_body = f'Name: {name}\nEmail: {email}\nMessage: {message}'
        from_email = settings.EMAIL_HOST_USER
        recipient_list = ['nmyaka@quantum-i.ai']

        try:
            send_mail(subject, message_body, from_email, recipient_list)
            messages.success(request, 'Your message has been sent successfully! Our support team will contact you soon.')
        except Exception as e:
            messages.error(request, 'There was an error sending your message. Please try again later.')

        return render(request, 'contact.html')

    return render(request, 'contact.html')

# import the feature functions
from feature_snippets import (
    get_stream_viewer_code,
    get_chatbot_code,
    get_anomaly_detection_code,
    get_text_classification_code,
    get_visualization_code
)


@csrf_exempt
def get_feature_code_plain(request, feature_name):
    # Map feature names to their corresponding view functions
    feature_map = {
        'stream_viewer': stream_viewer,
        'chatbot': chatbot,
        'anomaly_detection': anomaly_detection,
        'text_classification': text_classification,
        'visualization': databricks_dashboard_proxy,  # Assuming this is the visualization view
    }

    if feature_name not in feature_map:
        return HttpResponse("Feature not found", status=404, content_type="text/plain")

    try:
        # Get the source code of the specified view function
        source_code = inspect.getsource(feature_map[feature_name])
        return HttpResponse(source_code, content_type="text/plain")
    except Exception as e:
        logger.error(f"Error retrieving source code for {feature_name}: {str(e)}")
        return HttpResponse(f"Error retrieving source code: {str(e)}", status=500, content_type="text/plain")
    


from django.shortcuts import render
from datetime import datetime
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import pandas as pd
import json
import logging

# Set up logging
logger = logging.getLogger(__name__)

def forecast(request):
    # Hardcoded options (same as Streamlit)
    branch_options = [

    "Mumbai", "Delhi", "Bangalore", "Hyderabad", "Ahmedabad", "Chennai", "Kolkata", "Pune", "Jaipur", "Surat",

    "Lucknow", "Kanpur", "Nagpur", "Indore", "Thane", "Bhopal", "Visakhapatnam", "Patna", "Vadodara", "Ghaziabad",

    "Ludhiana", "Agra", "Nashik", "Faridabad", "Meerut", "Rajkot", "Kalyan-Dombivli", "Vasai-Virar", "Varanasi",

    "Srinagar", "Aurangabad", "Dhanbad", "Amritsar", "Navi Mumbai", "Allahabad", "Ranchi", "Howrah", "Coimbatore",

    "Jabalpur", "Gwalior", "Vijayawada", "Jodhpur", "Madurai", "Raipur", "Kota", "Guwahati", "Chandigarh", "Solapur",

    "Hubli–Dharwad", "Bareilly", "Moradabad", "Mysore", "Gurgaon", "Aligarh", "Jalandhar", "Tiruchirappalli",

    "Bhubaneswar", "Salem", "Mira-Bhayandar", "Thiruvananthapuram", "Bhiwandi", "Saharanpur", "Guntur", "Amravati",

    "Bikaner", "Noida", "Jamshedpur", "Bhilai", "Cuttack", "Firozabad", "Kochi", "Nellore", "Bhavnagar", "Dehradun",

    "Durgapur", "Asansol", "Rourkela", "Nanded", "Kolhapur", "Ajmer", "Akola", "Gulbarga", "Jamnagar", "Ujjain",

    "Loni", "Siliguri", "Jhansi", "Ulhasnagar", "Jammu", "Mangalore", "Belgaum", "Kurnool", "Ambattur",

    "Tirupati", "Malegaon", "Gaya", "Udaipur", "Maheshtala", "Davanagere", "Kozhikode", "Bokaro", "South Dumdum",

    "Bhimabaram", "Puri", "Warangal", "Trichi", "Tenali", "Khammam", "Palakad", "Berhampur"

]
 
    move_type_options = ["Local", "Short Haul", "Labor Only", "INTERSTATE", "Long Distance"]

    # Initialize variables
    error = None
    table_data = None
    chart_data = None
    min_date = datetime.now().strftime("%Y-%m-%d")
    selected_branch = ""
    selected_move_type = ""
    API_BASE_URL = "http://20.81.241.192:8003"

    def call_api(url, payload):
        session = requests.Session()
        retries = Retry(total=5, backoff_factor=2, status_forcelist=[502, 503, 504])
        session.mount("http://", HTTPAdapter(max_retries=retries))
        try:
            response = session.post(url, json=payload, timeout=200)
            response.raise_for_status()
            return response.json()
        except requests.exceptions.HTTPError as e:
            try:
                error_detail = e.response.json().get("detail", e.response.text)
            except ValueError:
                error_detail = e.response.text
            logger.error(f"API Error at {url}: {error_detail}")
            return {"error": f"API Error (Status {e.response.status_code}): {error_detail}"}
        except requests.exceptions.RequestException as e:
            logger.error(f"Network Error at {url}: {str(e)}")
            return {"error": f"Network Error: Unable to connect to the API. {str(e)}"}

    if request.method == "POST":
        date = request.POST.get("date")
        branch = request.POST.get("branch")
        move_type = request.POST.get("move_type")

        # Validate branch
        if not branch or branch == "":
            error = "Please select a valid Branch."
        else:
            payload = {
                "date": date,
                "branch": branch,
                "move_type": move_type if move_type and move_type != "" else None
            }
            # Call APIs
            forecast_result = call_api(f"{API_BASE_URL}/forecast/", payload)
            historical_result = call_api(f"{API_BASE_URL}/historical_trends/", payload)

            if "error" in forecast_result:
                error = forecast_result["error"]
            elif "error" in historical_result:
                error = historical_result["error"]
            else:
                # Process table data
                predicted_summary = forecast_result.get("predicted_summary", [])
                if not predicted_summary:
                    error = "No forecast data returned from the API."
                else:
                    table_data = [
                        {
                            "date": item["date"],
                            "predicted_moves": item["predicted_moves"],
                            "comment": item["comment"]
                        }
                        for item in predicted_summary
                    ]
                    logger.debug(f"Table Data: {table_data}")

                # Process chart data
                if not error:
                    try:
                        # Forecast data
                        forecast_dates = [item["date"] for item in predicted_summary]
                        forecast_moves = [item["predicted_moves"] for item in predicted_summary]

                        # Historical data
                        historical_trends = historical_result.get("historical_trends", [])
                        if not historical_trends:
                            error = "No historical trends data returned from the API."
                        else:
                            date_moves = {}
                            for year_data in historical_trends:
                                for data_point in year_data["data"]:
                                    date_str = data_point["date"]  # MM-DD
                                    moves = data_point["moves"]
                                    if date_str not in date_moves:
                                        date_moves[date_str] = []
                                    date_moves[date_str].append(moves)

                            # Compute average historical moves
                            historical_avg = []
                            historical_dates = []
                            window_start = pd.to_datetime(historical_result["window"]["start_date"])
                            window_end = pd.to_datetime(historical_result["window"]["end_date"])
                            date_range = pd.date_range(window_start, window_end, freq="D")
                            for date in date_range:
                                date_str = date.strftime("%m-%d")
                                if date_str in date_moves:
                                    avg_moves = sum(date_moves[date_str]) / len(date_moves[date_str])
                                    historical_avg.append(avg_moves)
                                else:
                                    historical_avg.append(0)
                                historical_dates.append(date.strftime("%Y-%m-%d"))

                            chart_data = {
                                "forecast": {
                                    "dates": forecast_dates,
                                    "moves": forecast_moves
                                },
                                "historical": {
                                    "dates": historical_dates,
                                    "moves": historical_avg
                                },
                                "title": f"Move Trends for {branch}{' - ' + move_type if move_type else ''}"
                            }
                            logger.debug(f"Chart Data: {chart_data}")
                    except Exception as e:
                        logger.error(f"Chart Data Processing Error: {str(e)}")
                        error = f"Error processing chart data: {str(e)}"

                selected_branch = branch
                selected_move_type = move_type

    # Render template with context
    return render(request, "forecast.html", {
        "branch_options": branch_options,
        "move_type_options": move_type_options,
        "min_date": min_date,
        "error": error,
        "table_data": table_data,
        "chart_data": json.dumps(chart_data, default=str) if chart_data else None,
        "selected_branch": selected_branch,
        "selected_move_type": selected_move_type
    })
