from django.shortcuts import render, redirect
from django.contrib import messages
from django.core.cache import cache
from .models import UploadedFile
from django.conf import settings
import hashlib
import logging
import time
import json
from io import StringIO
import os
import datetime
from django.core.paginator import Paginator
from django.core.cache import cache
from pyspark.sql import SparkSession
from pyspark.sql.functions import to_date
from django.http import JsonResponse, HttpResponse, HttpResponseServerError
from django.views.decorators.csrf import csrf_exempt 
from urllib.parse import urlencode
# from databricks.sql import connect
from databricks.connect import DatabricksSession
from azure.storage.filedatalake import DataLakeServiceClient
import requests
import pandas as pd

# Configure logging
logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)

# Initialize a Boolean to track if a file has been uploaded in the current server session
file_uploaded = False

def check_file_uploaded():
    """Check if a file has been uploaded in the current session."""
    logger.debug(f"File uploaded status: {file_uploaded}")
    return file_uploaded

def home(request):
    return render(request, 'home.html')

def log_analytics(request):
    global file_uploaded
    if request.method == 'POST':
        if 'show_analysis' in request.POST:
            # If "Show Analysis" button is clicked, check the file_uploaded Boolean
            if check_file_uploaded():
                return redirect('analysis_options')
            else:
                messages.error(request, "Please upload a file first.")
                return render(request, 'log_analytics.html')
        
        uploaded_file = request.FILES.get('csv_file')

        if not uploaded_file:
            messages.error(request, "No file uploaded.")
            return render(request, 'log_analytics.html')

        valid_extensions = ('.csv', '.log', '.txt')
        if not uploaded_file.name.lower().endswith(valid_extensions):
            messages.error(request, "Only CSV, log, or text files are allowed.")
            return render(request, 'log_analytics.html')

        account_name = settings.AZURE_ACCOUNT_NAME
        account_key = settings.AZURE_ACCOUNT_KEY
        file_system = settings.AZURE_FILE_SYSTEM
        directory = "catalogiq"
        tracking_file = "uploaded_files.txt"

        try:
            service_client = DataLakeServiceClient(
                account_url=f"https://{account_name}.dfs.core.windows.net",
                credential=account_key
            )
            file_system_client = service_client.get_file_system_client(file_system)
            try:
                directory_client = file_system_client.get_directory_client(directory)
                directory_client.get_directory_properties()
            except:
                directory_client = file_system_client.create_directory(directory)
        except Exception as e:
            logger.error(f"ADLS Gen2 connection failed: {str(e)}")
            messages.error(request, f"Storage connection failed: {str(e)}")
            return render(request, 'log_analytics.html')

        # Check if file already exists in ADLS Gen2
        file_exists = False
        try:
            existing_file_client = directory_client.get_file_client(uploaded_file.name)
            existing_file_client.get_file_properties()
            file_exists = True
        except:
            file_exists = False

        if file_exists:
            # File already exists, update metadata and enable analysis
            messages.info(request, f"File '{uploaded_file.name}' already uploaded.")
            try:
                uploaded_file_obj, created = UploadedFile.objects.get_or_create(filename=uploaded_file.name)
                uploaded_file_obj.created_at = datetime.datetime.now()
                uploaded_file_obj.analyzed = False
                uploaded_file_obj.output_path = None
                uploaded_file_obj.save()
            except Exception as e:
                logger.error(f"Failed to save metadata for existing file: {str(e)}")

            # Set file_uploaded to True
            file_uploaded = True
            logger.debug(f"Set file_uploaded to True for existing file {uploaded_file.name}")
            return render(request, 'log_analytics.html')

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
            return render(request, 'log_analytics.html')

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
            return render(request, 'log_analytics.html')

        # Update uploaded_files.txt
        try:
            tracking_client = directory_client.get_file_client(tracking_file)
            try:
                existing_content = tracking_client.download_file().readall().decode('utf-8')
            except:
                existing_content = ""
            new_content = existing_content + uploaded_file.name + "\n"
            tracking_client.upload_data(data=new_content.encode(), overwrite=True)
        except Exception as e:
            logger.warning(f"Failed to update tracking file: {str(e)}")
            # Don't block upload on this

        # Update model
        try:
            uploaded_file_obj, created = UploadedFile.objects.get_or_create(filename=uploaded_file.name)
            uploaded_file_obj.created_at = datetime.datetime.now()
            uploaded_file_obj.analyzed = False
            uploaded_file_obj.output_path = None
            uploaded_file_obj.save()
        except Exception as e:
            logger.error(f"Failed to save metadata: {str(e)}")

        # Set the file_uploaded Boolean to True
        file_uploaded = True
        logger.debug(f"Set file_uploaded to True after uploading {uploaded_file.name}")

        # Clean up
        if os.path.exists(temp_path):
            os.remove(temp_path)

        messages.success(request, f"File '{uploaded_file.name}' uploaded successfully to ADLS Gen2!")
        return redirect('log_analytics')

    return render(request, 'log_analytics.html')

def analysis_options(request):
    logger.debug("Rendering analysis_options.html")
    file_uploaded_status = check_file_uploaded()
    logger.debug(f"File uploaded status in analysis_options: {file_uploaded_status}")
    return render(request, 'analysis_options.html', {'file_uploaded': file_uploaded_status})

def anomaly_detection(request):
    logger.debug("Handling anomaly_detection request")
    if not check_file_uploaded():
        messages.error(request, "Please upload a file first.")
        return redirect('log_analytics')

    # Superset iframe URL
    superset_iframe_url = (
        "https://579f-223-184-64-14.ngrok-free.app/superset/dashboard/6a36c303-a40b-41c2-a699-490dfd3f2073/?standalone=true"
    )

    # Add any query parameters from the frontend request if needed
    params = request.GET.dict()
    if params:
        superset_iframe_url += "&" + urlencode(params)

    logger.debug(f"Rendering Superset iframe with URL: {superset_iframe_url}")
    
    try:
        return render(request, 'anomaly_detection.html', {'iframe_url': superset_iframe_url})
    except Exception as e:
        logger.error(f"Error rendering dashboard: {str(e)}")
        messages.error(request, "Failed to load the dashboard. Please try again later.")
        return redirect('log_analytics')

def stream_viewer(request):
    logger.debug("Handling stream_viewer request")
    if not check_file_uploaded():
        messages.error(request, "Please upload a file first.")
        return redirect('log_analytics')
 
    # Table configuration
    table_name = "text_log_analytics_catalog.silver_schema.silver_processed_logs"
 
    # Pagination parameters
    page_size = 20
    page = request.GET.get('page', '1')
    try:
        page = int(page) if page.isdigit() else 1
        if page < 1:
            page = 1
    except ValueError:
        page = 1

    try:
        # Connect to Databricks
        spark = DatabricksSession.builder.remote(
            host=settings.DATABRICKS_HOST,
            token=settings.DATABRICKS_TOKEN,
            cluster_id=settings.DATABRICKS_CLUSTER_ID
        ).getOrCreate()

        # Read data from the catalog table
        df = spark.table(table_name).cache()
        logger.debug(f"Table {table_name} loaded and cached")

        # Get all columns from the table
        available_columns = df.columns
        if not available_columns:
            logger.error(f"No columns found in table {table_name}")
            messages.error(request, "No columns found in data.")
            return render(request, 'stream_viewer.html', {
                'results': [],
                'columns': [],
                'filter_values': {'level': []},
                'pagination': {},
                'current_filters': {}
            })
        logger.debug(f"Available columns: {available_columns}")

        # Select all columns
        df = df.select(available_columns)

        # Get unique level values for dropdown (cached), if 'level' or 'log_level' exists
        level_column = 'level' if 'level' in available_columns else 'log_level' if 'log_level' in available_columns else None
        cache_key_levels = f"stream_viewer_levels_{table_name}"
        level_values = cache.get(cache_key_levels)
        if not level_values:
            if level_column:
                level_values = df.select(level_column).distinct().dropna().limit(100).collect()
                level_values = sorted([row[level_column] for row in level_values if row[level_column] is not None])
                cache.set(cache_key_levels, level_values, timeout=3600)
            else:
                level_values = []
            logger.debug(f"Cached level values: {level_values}")

        # Collect data (limited to reduce memory usage)
        data_rows = df.limit(1000).collect()
        data_list = [row.asDict() for row in data_rows]
        logger.debug(f"Collected {len(data_list)} rows from table {table_name}")

        # Convert to pandas DataFrame for local filtering
        pandas_df = pd.DataFrame(data_list)
        if pandas_df.empty:
            logger.warning(f"No data collected from table {table_name}")
            messages.info(request, "No data available to display.")
            return render(request, 'stream_viewer.html', {
                'results': [],
                'columns': available_columns,
                'filter_values': {'level': level_values},
                'pagination': {},
                'current_filters': {}
            })

        # Apply local filters
        filtered_df = pandas_df.copy()
        current_filters = {}
        level_filter = request.GET.get("level")
        if level_filter and level_column and level_filter in level_values:
            filtered_df = filtered_df[filtered_df[level_column] == level_filter]
            current_filters['level'] = level_filter
            logger.debug(f"Applied level filter: {level_filter}")
        tag_filter = request.GET.get("tag")
        if tag_filter and 'tag' in available_columns:
            filtered_df = filtered_df[filtered_df['tag'].str.contains(tag_filter, case=False, na=False)]
            current_filters['tag'] = tag_filter
            logger.debug(f"Applied tag filter: {tag_filter}")
        message_filter = request.GET.get("message")
        if message_filter and 'message' in available_columns:
            filtered_df = filtered_df[filtered_df['message'].str.contains(message_filter, case=False, na=False)]
            current_filters['message'] = message_filter
            logger.debug(f"Applied message filter: {message_filter}")

        # Log filtered DataFrame size
        logger.debug(f"Filtered DataFrame size: {len(filtered_df)} rows")

        # Pagination
        results = filtered_df.to_dict('records')
        total_records = len(results)
        paginator = Paginator(results, page_size)
        try:
            page_obj = paginator.page(page)
        except:
            page_obj = paginator.page(1)
            page = 1
        results = page_obj.object_list
        logger.debug(f"Paginated results: {len(results)} records for page {page}")

        # Pagination metadata
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

        # Prepare filter values for template
        filter_values = {
            'level': level_values,
            'tag': [],
            'message': []
        }

        if not results and any(current_filters.values()):
            messages.info(request, "No records match the selected filters.")
            logger.debug("No records match the applied filters")

        logger.debug(f"Rendering stream_viewer with {len(results)} records, page {page}")
    except Exception as e:
        logger.error(f"Failed to query table {table_name}: {str(e)}", exc_info=True)
        messages.error(request, f"Failed to load stream data: {str(e)}")
        return render(request, 'stream_viewer.html', {
            'results': [],
            'columns': [],
            'filter_values': {'level': []},
            'pagination': {},
            'current_filters': {}
        })

    return render(request, 'stream_viewer.html', {
        'results': results,
        'columns': available_columns,
        'filter_values': filter_values,
        'pagination': pagination,
        'current_filters': current_filters
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

            # Log token and headers for debugging
            logger.debug(f"Databricks Token: {settings.DATABRICKS_TOKEN}")
            endpoint_url = "https://adb-3623933893880845.5.azuredatabricks.net/serving-endpoints/LogIQ/invocations"
            headers = {
                "Authorization": f"Bearer {settings.DATABRICKS_TOKEN}",
                "Content-Type": "application/json"
            }
            logger.debug(f"Request Headers: {headers}")

            # Construct payload in the required dataframe_split format
            payload = {
                "dataframe_split": {
                    "columns": ["input", "tool_name"],
                    "data": [[query, "android_log"]]
                }
            }
            logger.debug(f"Request Payload: {payload}")

            response = requests.post(endpoint_url, headers=headers, json=payload, timeout=30)
            logger.debug(f"Databricks Response Status: {response.status_code}")
            logger.debug(f"Databricks Response: {response.text}")

            if response.status_code == 200:
                result = response.json()
                # Extract the output from the nested predictions structure
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
    
    return render(request, 'chatbot.html')

NGROK_BASE = "https://5b65-223-184-64-14.ngrok-free.app/superset/dashboard/2751f42b-c289-4698-bde5-1c3994da9b1f/?standalone=true"  # your ngrok tunnel URL

def superset_proxy(request, path):
    # Build the full URL
    url = f"{NGROK_BASE}/{path}"
    # Forward headers, add ngrok skip header
    headers = {
        "ngrok-skip-browser-warning": "1",
        **{k: v for k, v in request.headers.items() if k not in ("Host",)},
    }
    # Forward GET (you can extend for POST if needed)
    resp = requests.get(url, headers=headers, params=request.GET, stream=True)
    # Build Django response
    django_resp = HttpResponse(
        resp.raw,
        status=resp.status_code,
        content_type=resp.headers.get("Content-Type", "text/html"),
    )
    return django_resp


def databricks_dashboard_proxy(request):
    # Check if a file is uploaded (assuming check_file_uploaded is defined elsewhere)
    if not check_file_uploaded():
        messages.error(request, "Please upload a file first.")
        return redirect('log_analytics')

    # Superset iframe URL
    superset_iframe_url = (
        "https://579f-223-184-64-14.ngrok-free.app/superset/dashboard/6a36c303-a40b-41c2-a699-490dfd3f2073/?standalone=true"
    )

    # Add any query parameters from the frontend request if needed
    params = request.GET.dict()
    if params:
        superset_iframe_url += "&" + urlencode(params)

    logger.debug(f"Rendering Superset iframe with URL: {superset_iframe_url}")
    
    try:
        return render(request, 'dashboard.html', {'iframe_url': superset_iframe_url})
    except Exception as e:
        logger.error(f"Error rendering dashboard: {str(e)}")
        messages.error(request, "Failed to load the dashboard. Please try again later.")
        return redirect('log_analytics')
        
def about_us(request):
    return render(request, 'about_us.html')

def contact(request):
    if request.method == 'POST':
        # Handle form submission
        name = request.POST.get('name')
        email = request.POST.get('email')
        message = request.POST.get('message')
           # Add your logic to process the form (e.g., save to database, send email)
        messages.success(request, "Thank you for your message! We'll get back to you soon.")
        return redirect('contact')
    return render(request, 'contact.html')

@csrf_exempt
def text_classification(request):
    logger.debug("Handling text_classification request")
    if request.method == 'POST':
        try:
            data = json.loads(request.body)
            message_type = data.get('message_type', '')
            text = data.get('text', '')
            subject = data.get('subject', None) # New: Get subject for email
            logger.debug(f"Received text classification request: message_type={message_type}, text={text}, subject={subject}")
            # Validation: if email, subject is also required
            if message_type == 'email' and (not text and not subject):
                return JsonResponse({'response': 'Please provide both subject and message for email classification.'}, status=400)
            elif not text and message_type != 'email': # For SMS/WhatsApp, only text is needed
                return JsonResponse({'response': 'Please provide text to classify.'}, status=400)
            # Log token and headers for debugging
            logger.debug(f"Databricks Token: {settings.DATABRICKS_TOKEN}")
            endpoint_url = "https://adb-3623933893880845.5.azuredatabricks.net/serving-endpoints/ChannelSort/invocations"
            headers = {
                "Authorization": f"Bearer {settings.DATABRICKS_TOKEN}",
                "Content-Type": "application/json"
            }
            logger.debug(f"Request Headers: {headers}")
            # Construct payload in the required dataframe_split format based on message_type
            # We need to send 'null' for fields that are not applicable to the message type
            # The order of columns and data values must match the Databricks endpoint's expectation.
            # Based on your provided request example: ["source_type", "subject", "body", "priority", "message"]
            payload_columns = ["source_type", "subject", "body", "priority", "message"]
            payload_data = []
            if message_type == 'email':
                payload_data.append([
                    message_type,
                    subject if subject else "null", # Use actual subject or "null"
                    text, # Assuming 'body' is the main message content for email
                    "high", # Default priority, you might want to make this dynamic
                    None # 'message' is null for email in your example
                ])
            elif message_type == 'sms':
                payload_data.append([
                    message_type,
                    None, # Subject is null for SMS
                    None, # Body is null for SMS
                    "high", # Default priority
                    text # 'message' is the main content for SMS
                ])
            elif message_type == 'whatsapp':
                payload_data.append([
                    message_type,
                    None, # Subject is null for WhatsApp
                    None, # Body is null for WhatsApp
                    "medium", # Default priority
                    text # 'message' is the main content for WhatsApp
                ])
            payload = {
                "dataframe_split": {
                    "columns": payload_columns,
                    "data": payload_data
                }
            }
            logger.debug(f"Request Payload: {payload}")
            response = requests.post(endpoint_url, headers=headers, json=payload, timeout=30)
            logger.debug(f"Databricks Response Status: {response.status_code}")
            logger.debug(f"Databricks Response: {response.text}")
            if response.status_code == 200:
                result = response.json()
                predictions = result.get('predictions', [])
                bot_response = "Classification failed." # Default message
                if predictions and isinstance(predictions, list) and len(predictions) > 0:
                    # Assuming we are interested in the first prediction for the single input
                    first_prediction = predictions[0]
                    source_type_out = first_prediction.get('source_type', 'N/A')
                    queue = first_prediction.get('queue', 'N/A')
                    # Get the input message (original text sent) from the 'input' dictionary within the prediction
                    # Adjusting based on the sample response structure: 'input' is a dictionary.
                    # We need to decide which field from 'input' to display as 'Message'.
                    # For email, it could be 'body' or 'message'. For SMS/WhatsApp, it's 'message'.
                    # Let's try to get the 'message' field first, then 'body', then the original 'text' sent.
                    input_data = first_prediction.get('input', {})
                    display_message = input_data.get('message') or input_data.get('body') or text
                    # Format the response as requested, including the subject if it was an email
                    if source_type_out.lower() == 'email' and subject: # Check if subject was provided and it's an email
                        bot_response = (
                            f"Source Type = {source_type_out}\n"
                            f"Subject = {subject}\n" # Display the subject here
                            f"Message = {display_message}\n"
                            f"Classified as = \"{queue}\""
                        )
                    else:
                        bot_response = (
                            f"Source Type = {source_type_out}\n"
                            f"Message = {display_message}\n"
                            f"Classified as = \"{queue}\""
                        )
                else:
                    bot_response = 'Sorry, no classification predictions were found.'
            else:
                logger.error(f"Databricks endpoint error: {response.text}")
                try:
                    error_message = response.json().get('message', 'Unknown error from service.')
                except json.JSONDecodeError:
                    error_message = response.text
                bot_response = f"Error from service: {error_message}"
            return JsonResponse({'response': bot_response})
        except Exception as e:
            logger.error(f"Text classification error: {str(e)}", exc_info=True)
            return JsonResponse({'response': f"Error: {str(e)}"}, status=500)
    return render(request, 'text_classification.html')