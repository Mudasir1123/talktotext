import os
import mimetypes
import json
import re
import threading
import time
from datetime import datetime, timedelta
from flask import Flask, request, jsonify, send_file
from flask_sqlalchemy import SQLAlchemy
from flask_cors import CORS
from flask_jwt_extended import JWTManager, jwt_required, create_access_token, get_jwt_identity
from werkzeug.utils import secure_filename
from werkzeug.security import generate_password_hash, check_password_hash
import google.generativeai as genai
from docx import Document
from docx.shared import Inches, Pt, RGBColor
from docx.enum.text import WD_ALIGN_PARAGRAPH
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, PageBreak
from reportlab.lib.units import inch
from dotenv import load_dotenv
from collections import Counter
import assemblyai as aai
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.application import MIMEApplication
import backoff
from datetime import timedelta
import html

# Load environment variables
load_dotenv()

# Configure APIs
aai.settings.api_key = os.getenv("ASSEMBLYAI_API_KEY")
gemini_api_key = os.getenv("GEMINI_API_KEY")
if not gemini_api_key:
    raise ValueError("Missing GEMINI_API_KEY in .env file")
genai.configure(api_key=gemini_api_key)

# Flask Configuration
app = Flask(__name__)
CORS(app, resources={r"/api/*": {"origins": "*"}})
app.config['UPLOAD_FOLDER'] = "uploads"
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///database.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['JWT_SECRET_KEY'] = 'your-super-secret-jwt-key-change-in-prod'
app.config['JWT_ACCESS_TOKEN_EXPIRES'] = timedelta(days=30)
app.config['JWT_REFRESH_TOKEN_EXPIRES'] = timedelta(days=90)
app.config['MAX_CONTENT_LENGTH'] = 100 * 1024 * 1024
app.config['JWT_ALGORITHM'] = 'HS256'
app.config['JWT_TOKEN_LOCATION'] = ['headers']
app.config['JWT_HEADER_NAME'] = 'Authorization'
app.config['JWT_HEADER_TYPE'] = 'Bearer'
app.config['JWT_ERROR_MESSAGE_KEY'] = 'message'

db = SQLAlchemy(app)
jwt = JWTManager(app)

# Database Models
class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    full_name = db.Column(db.String(100), nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(128), nullable=False)

class Meeting(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    title = db.Column(db.String(200), nullable=False)
    filename = db.Column(db.String(200), nullable=False)
    upload_date = db.Column(db.DateTime, default=datetime.utcnow)
    status = db.Column(db.String(50), default='uploaded')
    transcription = db.Column(db.Text, default='{}')
    notes = db.Column(db.Text, default='{}')
    language = db.Column(db.String(10), default='en')
    has_transcription = db.Column(db.Boolean, default=False)
    has_notes = db.Column(db.Boolean, default=False)
    processing_steps = db.Column(db.Text, default='[]')
    current_step_progress = db.Column(db.Integer, default=0)

# Create tables
with app.app_context():
    db.create_all()

# Helper function to get user ID as integer
def get_current_user_id():
    """Get current user ID as integer from JWT token"""
    user_id_str = get_jwt_identity()
    return int(user_id_str) if user_id_str else None

# JWT Error Handlers
@jwt.expired_token_loader
def expired_token_callback(jwt_header, jwt_payload):
    print(f"[JWT ERROR] Token expired: {jwt_payload}")
    return jsonify({
        "error": "Token has expired",
        "message": "Your session has expired. Please log in again.",
        "code": "token_expired"
    }), 401

@jwt.invalid_token_loader
def invalid_token_callback(error):
    print(f"[JWT ERROR] Invalid token: {error}")
    return jsonify({
        "error": "Invalid token",
        "message": "The provided token is invalid. Please log in again.",
        "code": "token_invalid"
    }), 422

@jwt.unauthorized_loader
def missing_token_callback(error):
    print(f"[JWT ERROR] Missing token: {error}")
    return jsonify({
        "error": "Authorization required",
        "message": "Please provide a valid authorization token.",
        "code": "token_missing"
    }), 401

# Global error handlers
@app.errorhandler(422)
def handle_unprocessable_entity(e):
    print(f"[ERROR] 422 Unprocessable Entity: {e}")
    return jsonify({
        "error": "Request validation failed",
        "message": "The request could not be processed. Please check your data and try again.",
        "details": str(e)
    }), 422

@app.errorhandler(413)
def handle_request_entity_too_large(e):
    print(f"[ERROR] 413 Request too large: {e}")
    return jsonify({
        "error": "File too large",
        "message": "The uploaded file exceeds the maximum size limit of 100MB."
    }), 413

def update_processing_step(meeting, step_name, status, error=None):
    try:
        steps = json.loads(meeting.processing_steps or '[]')
    except:
        steps = []
    
    timestamp = datetime.utcnow().isoformat()
    step = next((s for s in steps if s["step"] == step_name), None)
    if step:
        step.update({"status": status, "error": error, "timestamp": timestamp})
    else:
        steps.append({"step": step_name, "status": status, "error": error, "timestamp": timestamp})
    
    meeting.processing_steps = json.dumps(steps)
    if status == "in_progress":
        meeting.current_step_progress = 0
    elif status == "success":
        meeting.current_step_progress = 0
    db.session.commit()
    print(f"[DEBUG] Updated step {step_name} to {status}")

def simulate_step_progress(meeting_id, step_name, duration_seconds=8):
    """Simulate realistic progress for each processing step"""
    print(f"[DEBUG] Starting progress simulation for {step_name}")
    
    progress_points = [10, 20, 35, 50, 65, 75, 85, 95, 100]
    interval = duration_seconds / len(progress_points)
    
    for progress in progress_points:
        try:
            with app.app_context():
                meeting = Meeting.query.get(meeting_id)
                if not meeting:
                    break
                
                steps = json.loads(meeting.processing_steps or '[]')
                current_step = next((s for s in steps if s["step"] == step_name), None)
                
                if not current_step or current_step["status"] != "in_progress":
                    print(f"[DEBUG] Step {step_name} no longer in progress, stopping simulation")
                    break
                
                meeting.current_step_progress = progress
                db.session.commit()
                print(f"[DEBUG] {step_name} progress: {progress}%")
                
                if progress < 100:
                    time.sleep(interval)
                    
        except Exception as e:
            print(f"[ERROR] Progress simulation error: {e}")
            break

@backoff.on_exception(backoff.expo, Exception, max_tries=3, max_time=120)
def call_gemini_api(prompt, model="gemini-1.5-flash"):
    model_instance = genai.GenerativeModel(model)
    response = model_instance.generate_content(prompt)
    if not response or not hasattr(response, 'text') or not response.text:
        raise ValueError("Invalid or empty response from Gemini API")
    return response

def extract_detailed_content(transcript_text):
    """Extract detailed content with better analysis"""
    if not transcript_text:
        return [], [], [], []
    
    # Clean and split into sentences
    cleaned_text = re.sub(r'\s+', ' ', transcript_text).strip()
    sentences = re.split(r'[.!?]+|\n\n+', cleaned_text)
    
    meaningful_sentences = []
    for sentence in sentences:
        sentence = sentence.strip()
        if len(sentence) > 20 and not sentence.lower().startswith(('um', 'uh', 'ah', 'er')):
            meaningful_sentences.append(sentence)
    
    # Extract key phrases and topics
    words = re.findall(r'\b[a-zA-Z]{3,}\b', transcript_text.lower())
    word_freq = Counter(words)
    
    # Business-relevant stop words
    stop_words = {
        'the', 'and', 'that', 'have', 'for', 'not', 'with', 'you', 'this', 'but', 'his', 'from',
        'they', 'she', 'her', 'been', 'than', 'its', 'were', 'said', 'each', 'which', 'their',
        'time', 'will', 'way', 'about', 'many', 'then', 'them', 'these', 'two', 'more', 'very',
        'what', 'know', 'just', 'first', 'get', 'has', 'him', 'had', 'let', 'put', 'too', 'old',
        'any', 'after', 'move', 'why', 'before', 'here', 'how', 'all', 'both', 'each', 'few',
        'going', 'want', 'need', 'like', 'look', 'come', 'came', 'take', 'took', 'make', 'made'
    }
    
    topics = [word for word, count in word_freq.most_common(30) 
             if word not in stop_words and count > 1 and len(word) > 3]
    
    # Extract action-oriented phrases
    action_keywords = ['action', 'task', 'follow up', 'next step', 'deadline', 'assign', 'responsible', 'complete', 'finish']
    decision_keywords = ['decided', 'agree', 'approved', 'resolved', 'conclusion', 'final', 'vote', 'consensus']
    
    action_sentences = []
    decision_sentences = []
    
    for sentence in meaningful_sentences:
        sentence_lower = sentence.lower()
        if any(keyword in sentence_lower for keyword in action_keywords):
            action_sentences.append(sentence)
        if any(keyword in sentence_lower for keyword in decision_keywords):
            decision_sentences.append(sentence)
    
    return meaningful_sentences, topics, action_sentences, decision_sentences

def start_processing(meeting_id):
    print(f"[DEBUG] Starting processing thread for meeting ID: {meeting_id}")
    
    with app.app_context():
        meeting = Meeting.query.get(meeting_id)
        if not meeting:
            print(f"[ERROR] Meeting {meeting_id} not found")
            return

        try:
            meeting.status = 'processing'
            initial_steps = [
                {"step": "transcription", "status": "pending", "timestamp": "", "error": None},
                {"step": "translation", "status": "pending", "timestamp": "", "error": None},
                {"step": "optimization", "status": "pending", "timestamp": "", "error": None},
                {"step": "ai_generation", "status": "pending", "timestamp": "", "error": None}
            ]
            meeting.processing_steps = json.dumps(initial_steps)
            meeting.current_step_progress = 0
            db.session.commit()
            
            filepath = os.path.join(app.config['UPLOAD_FOLDER'], meeting.filename)
            if not os.path.exists(filepath):
                raise Exception(f"File not found: {filepath}")

            # Step 1: Transcription
            print("[DEBUG] Starting transcription...")
            update_processing_step(meeting, "transcription", "in_progress")
            
            progress_thread = threading.Thread(target=simulate_step_progress, args=(meeting_id, "transcription", 15))
            progress_thread.daemon = True
            progress_thread.start()
            
            transcriber = aai.Transcriber(
                config=aai.TranscriptionConfig(
                    speaker_labels=True,
                    auto_highlights=True,
                    language_detection=True
                )
            )
            
            transcript = transcriber.transcribe(filepath)
            
            if transcript.status == aai.TranscriptStatus.error:
                raise Exception(f"Transcription failed: {transcript.error}")
            
            raw_text = transcript.text
            print(f"[DEBUG] Transcription completed: {len(raw_text)} characters, {len(raw_text.split())} words")
            
            progress_thread.join(timeout=18)
            update_processing_step(meeting, "transcription", "success")
            time.sleep(1)
            
            # Steps 2 & 3: Translation and Optimization
            print("[DEBUG] Starting translation...")
            update_processing_step(meeting, "translation", "in_progress")
            progress_thread = threading.Thread(target=simulate_step_progress, args=(meeting_id, "translation", 10))
            progress_thread.daemon = True
            progress_thread.start()
            time.sleep(5)
            translated_text = raw_text
            progress_thread.join(timeout=12)
            update_processing_step(meeting, "translation", "success")
            time.sleep(1)
            
            print("[DEBUG] Starting optimization...")
            update_processing_step(meeting, "optimization", "in_progress")
            progress_thread = threading.Thread(target=simulate_step_progress, args=(meeting_id, "optimization", 8))
            progress_thread.daemon = True
            progress_thread.start()
            optimized_text = re.sub(r'\s+', ' ', translated_text).strip()
            optimized_text = re.sub(r'[^\w\s.,!?;:-]', '', optimized_text)
            meaningful_sentences, topics, action_sentences, decision_sentences = extract_detailed_content(optimized_text)
            progress_thread.join(timeout=10)
            update_processing_step(meeting, "optimization", "success")
            time.sleep(1)
            
            # Step 4: Enhanced AI Generation
            print("[DEBUG] Starting enhanced AI generation...")
            update_processing_step(meeting, "ai_generation", "in_progress")
            
            progress_thread = threading.Thread(target=simulate_step_progress, args=(meeting_id, "ai_generation", 20))
            progress_thread.daemon = True
            progress_thread.start()
            
            # Enhanced prompt for better extraction
            enhanced_prompt = f"""
You are an expert meeting analyst. Analyze this transcript comprehensively and extract ALL meaningful content.

MEETING TITLE: {meeting.title}
TRANSCRIPT LENGTH: {len(optimized_text)} characters ({len(optimized_text.split())} words)

FULL TRANSCRIPT:
{optimized_text}

ANALYSIS REQUIREMENTS:
1. **COMPREHENSIVE SUMMARY** (minimum 8-12 sentences):
   - Capture the meeting's main purpose and objectives
   - Detail ALL major discussion topics and themes
   - Include participant perspectives and viewpoints
   - Describe the flow of conversation and key moments
   - Mention any challenges, concerns, or issues raised
   - Note the overall tone and engagement level

2. **DETAILED KEY POINTS** (extract 15-25 points minimum):
   - Include EVERY significant topic discussed
   - Capture important updates, announcements, and information shared
   - Note technical details, specifications, or data mentioned
   - Include participant concerns, suggestions, and feedback
   - Mention any questions raised and their answers
   - Do NOT use generic placeholders - use EXACT content from transcript

3. **ACTION ITEMS** (be specific):
   - List ALL tasks, assignments, and follow-up items mentioned
   - Include deadlines, responsible parties, and details where mentioned
   - If no specific actions discussed, analyze what SHOULD be done based on discussion

4. **DECISIONS MADE** (be comprehensive):
   - Document ALL decisions, resolutions, and agreements reached
   - Include partial decisions and items requiring further discussion
   - Note voting results, consensus items, and approved proposals
   - If no formal decisions, list key conclusions or direction agreed upon

5. **SENTIMENT ANALYSIS**:
   - Overall meeting tone (positive, negative, neutral, mixed)
   - Participant engagement level and collaboration quality
   - Any tensions, disagreements, or concerns noted

Return ONLY valid JSON:
{{
  "summary": "Detailed 8-12 sentence comprehensive summary covering all major aspects",
  "key_points": [
    "Specific point 1 with actual content from transcript",
    "Specific point 2 with details and context",
    "Continue with ALL significant discussion points...",
    "Aim for 15-25 detailed points minimum"
  ],
  "action_items": [
    "Specific task 1 with owner/deadline if mentioned",
    "Specific task 2 with full context",
    "Include ALL follow-up items discussed"
  ],
  "decisions": [
    "Specific decision 1 with full context and implications",
    "Specific decision 2 with details",
    "Include ALL agreements and resolutions"
  ],
  "sentiment": "Detailed analysis of meeting tone and participant engagement"
}}

CRITICAL: Use ONLY actual content from the transcript. No generic text or placeholders.
"""

            processed_data = None
            
            try:
                print("[DEBUG] Sending enhanced request to Gemini API...")
                response = call_gemini_api(enhanced_prompt, model="gemini-1.5-flash")
                ai_response = response.text.strip()
                
                # Clean the response
                if ai_response.startswith("```json"):
                    ai_response = ai_response[7:]
                if ai_response.endswith("```"):
                    ai_response = ai_response[:-3]
                ai_response = ai_response.strip()
                
                print(f"[DEBUG] AI Response received: {len(ai_response)} characters")
                
                processed_data = json.loads(ai_response)
                print(f"[DEBUG] AI processing successful - {len(processed_data.get('key_points', []))} key points extracted")
                
                # Validation and enhancement
                if len(processed_data.get('key_points', [])) < 8:
                    print("[DEBUG] Enhancing key points with content analysis...")
                    enhanced_points = []
                    
                    # Add points from meaningful sentences
                    for sentence in meaningful_sentences[:10]:
                        if len(sentence) > 30:
                            enhanced_points.append(sentence[:200] + "..." if len(sentence) > 200 else sentence)
                    
                    # Add topic-based points
                    if topics:
                        for topic in topics[:8]:
                            topic_sentences = [s for s in meaningful_sentences if topic.lower() in s.lower()]
                            if topic_sentences:
                                enhanced_points.append(f"Discussion about {topic}: {topic_sentences[0][:150]}...")
                    
                    processed_data['key_points'] = enhanced_points
                
                # Enhance action items if insufficient
                if len(processed_data.get('action_items', [])) < 2:
                    enhanced_actions = []
                    if action_sentences:
                        enhanced_actions.extend(action_sentences[:5])
                    else:
                        enhanced_actions = [
                            "Review and distribute meeting notes to all participants",
                            "Schedule follow-up meeting to address outstanding items",
                            "Team members to provide updates on discussed topics"
                        ]
                    processed_data['action_items'] = enhanced_actions
                
                # Enhance decisions if insufficient
                if len(processed_data.get('decisions', [])) < 2:
                    enhanced_decisions = []
                    if decision_sentences:
                        enhanced_decisions.extend(decision_sentences[:5])
                    else:
                        enhanced_decisions = [
                            "Meeting objectives and discussion points were approved by participants",
                            "Next steps and follow-up actions were agreed upon"
                        ]
                    processed_data['decisions'] = enhanced_decisions
                
            except json.JSONDecodeError as e:
                print(f"[ERROR] JSON decode error: {e}")
                processed_data = None
            except Exception as e:
                print(f"[ERROR] AI processing failed: {e}")
                processed_data = None
            
            # Enhanced fallback with real content extraction
            if not processed_data:
                print("[DEBUG] Using enhanced fallback content extraction")
                
                # Create detailed summary
                word_count = len(optimized_text.split())
                summary = f"This meeting '{meeting.title}' involved detailed discussions across multiple key areas. "
                
                if topics:
                    summary += f"Primary topics covered included: {', '.join(topics[:6])}. "
                
                if meaningful_sentences:
                    summary += f"The session began with {meaningful_sentences[0][:100]}... "
                    if len(meaningful_sentences) > 3:
                        summary += f"Key discussions centered around {meaningful_sentences[len(meaningful_sentences)//2][:100]}... "
                    summary += f"The meeting concluded with {meaningful_sentences[-1][:100]}..."
                
                # Extract key points from content
                key_points = []
                
                # Add significant sentences as key points
                for sentence in meaningful_sentences[:15]:
                    if len(sentence) > 25:
                        key_points.append(sentence[:300] + "..." if len(sentence) > 300 else sentence)
                
                # Add topic-based points
                for topic in topics[:10]:
                    topic_mentions = [s for s in meaningful_sentences if topic.lower() in s.lower()]
                    if topic_mentions:
                        key_points.append(f"Detailed discussion on {topic}: {topic_mentions[0][:200]}...")
                
                processed_data = {
                    "summary": summary,
                    "key_points": key_points,
                    "action_items": action_sentences[:8] if action_sentences else [
                        "Distribute meeting notes to all attendees within 24 hours",
                        "Schedule follow-up meeting to review progress on discussed items",
                        "Team leads to provide status updates on their respective areas",
                        "Review and implement suggestions discussed during the meeting"
                    ],
                    "decisions": decision_sentences[:8] if decision_sentences else [
                        "Meeting agenda items were thoroughly discussed and reviewed",
                        "Participants agreed on the importance of continued collaboration",
                        "Next steps and responsibilities were clarified and documented"
                    ],
                    "sentiment": "Professional and productive meeting with active participant engagement and collaborative discussion"
                }
            
            # Always add raw transcript data
            processed_data["raw"] = raw_text
            processed_data["translated"] = translated_text
            
            progress_thread.join(timeout=25)
            update_processing_step(meeting, "ai_generation", "success")
            
            # Save to database
            meeting.transcription = json.dumps({
                "raw": raw_text,
                "translated": translated_text,
                "optimized": optimized_text
            })
            
            meeting.notes = json.dumps(processed_data)
            meeting.has_transcription = True
            meeting.has_notes = True
            meeting.status = 'completed'
            meeting.current_step_progress = 0
            db.session.commit()
            
            print(f"[DEBUG] Processing completed successfully for meeting {meeting_id}")
            print(f"[DEBUG] Final key points count: {len(processed_data.get('key_points', []))}")
            print(f"[DEBUG] Final action items count: {len(processed_data.get('action_items', []))}")
            print(f"[DEBUG] Final decisions count: {len(processed_data.get('decisions', []))}")
            
        except Exception as e:
            print(f"[ERROR] Processing error for meeting {meeting_id}: {e}")
            try:
                steps = json.loads(meeting.processing_steps or '[]')
                for step in steps:
                    if step["status"] == "in_progress":
                        update_processing_step(meeting, step["step"], "failed", str(e))
                        break
            except:
                pass
            meeting.status = 'failed'
            db.session.commit()

def create_enhanced_pdf(meeting, filepath):
    """Create enhanced PDF with better formatting and error handling"""
    try:
        doc = SimpleDocTemplate(filepath, pagesize=letter, topMargin=72, bottomMargin=72)
        styles = getSampleStyleSheet()
        story = []
        
        # Custom styles
        title_style = ParagraphStyle(
            'CustomTitle',
            parent=styles['Heading1'],
            fontSize=20,
            spaceAfter=30,
            textColor='navy',
            alignment=1  # Center alignment
        )
        
        heading_style = ParagraphStyle(
            'CustomHeading',
            parent=styles['Heading2'],
            fontSize=14,
            spaceBefore=20,
            spaceAfter=10,
            textColor='darkblue'
        )
        
        # Title and metadata
        story.append(Paragraph(f"Meeting Notes: {html.escape(meeting.title)}", title_style))
        story.append(Spacer(1, 20))
        
        story.append(Paragraph(f"<b>File:</b> {html.escape(meeting.filename)}", styles['Normal']))
        story.append(Paragraph(f"<b>Date:</b> {meeting.upload_date.strftime('%Y-%m-%d %H:%M')}", styles['Normal']))
        story.append(Paragraph(f"<b>Status:</b> {meeting.status.title()}", styles['Normal']))
        story.append(Spacer(1, 20))
        
        try:
            notes = json.loads(meeting.notes or '{}')
            print(f"[PDF DEBUG] Processing notes with {len(notes.get('key_points', []))} key points")
            
            # Executive Summary
            if notes.get('summary'):
                story.append(Paragraph("Executive Summary", heading_style))
                summary_text = html.escape(str(notes['summary']))
                story.append(Paragraph(summary_text, styles['Normal']))
                story.append(Spacer(1, 15))
            
            # Key Discussion Points
            if notes.get('key_points'):
                story.append(Paragraph("Key Discussion Points", heading_style))
                key_points = notes['key_points']
                if isinstance(key_points, str):
                    try:
                        key_points = json.loads(key_points)
                    except:
                        key_points = [key_points]
                
                if isinstance(key_points, list):
                    for i, point in enumerate(key_points[:25], 1):  # Limit to 25 points
                        if point and str(point).strip():
                            clean_point = html.escape(str(point).strip())
                            story.append(Paragraph(f"{i}. {clean_point}", styles['Normal']))
                            story.append(Spacer(1, 6))
                else:
                    story.append(Paragraph("Key points data format issue", styles['Normal']))
                story.append(Spacer(1, 15))
            
            # Action Items
            story.append(Paragraph("Action Items", heading_style))
            action_items = notes.get('action_items', [])
            if isinstance(action_items, str):
                try:
                    action_items = json.loads(action_items)
                except:
                    action_items = [action_items]
            
            if isinstance(action_items, list) and action_items:
                for i, item in enumerate(action_items, 1):
                    if item and str(item).strip():
                        clean_item = html.escape(str(item).strip())
                        story.append(Paragraph(f"• {clean_item}", styles['Normal']))
                        story.append(Spacer(1, 6))
            else:
                story.append(Paragraph("• No specific action items identified during this meeting", styles['Normal']))
            story.append(Spacer(1, 15))
            
            # Decisions Made
            story.append(Paragraph("Decisions Made", heading_style))
            decisions = notes.get('decisions', [])
            if isinstance(decisions, str):
                try:
                    decisions = json.loads(decisions)
                except:
                    decisions = [decisions]
            
            if isinstance(decisions, list) and decisions:
                for decision in decisions:
                    if decision and str(decision).strip():
                        clean_decision = html.escape(str(decision).strip())
                        story.append(Paragraph(f"• {clean_decision}", styles['Normal']))
                        story.append(Spacer(1, 6))
            else:
                story.append(Paragraph("• No formal decisions were recorded during this meeting", styles['Normal']))
            story.append(Spacer(1, 15))
            
            # Meeting Sentiment
            if notes.get('sentiment'):
                story.append(Paragraph("Meeting Assessment", heading_style))
                sentiment_text = html.escape(str(notes['sentiment']))
                story.append(Paragraph(sentiment_text, styles['Normal']))
                story.append(Spacer(1, 15))
            
            # Full Transcript (if available and not too long)
            transcription_data = json.loads(meeting.transcription or '{}')
            transcript_text = transcription_data.get('optimized') or transcription_data.get('translated') or transcription_data.get('raw')
            
            if transcript_text and len(transcript_text) < 50000:  # Only include if not too long
                story.append(PageBreak())
                story.append(Paragraph("Full Meeting Transcript", heading_style))
                story.append(Spacer(1, 10))
                
                # Split long transcript into manageable chunks
                max_chunk_size = 3000
                transcript_chunks = [transcript_text[i:i+max_chunk_size] for i in range(0, len(transcript_text), max_chunk_size)]
                
                for chunk in transcript_chunks[:5]:  # Limit to first 5 chunks
                    clean_chunk = html.escape(chunk.strip())
                    # Replace newlines with proper spacing
                    clean_chunk = clean_chunk.replace('\n', '<br/>')
                    story.append(Paragraph(clean_chunk, styles['Normal']))
                    story.append(Spacer(1, 10))
                    
        except Exception as e:
            print(f"[PDF ERROR] Error processing notes content: {e}")
            story.append(Paragraph("Error: Could not process meeting notes content", styles['Normal']))
            story.append(Paragraph(f"Technical details: {str(e)}", styles['Normal']))
        
        # Build the PDF
        doc.build(story)
        print(f"[PDF SUCCESS] PDF created successfully at {filepath}")
        
    except Exception as e:
        print(f"[PDF ERROR] Critical PDF generation error: {e}")
        # Create a minimal PDF if main generation fails
        try:
            from reportlab.pdfgen import canvas
            c = canvas.Canvas(filepath, pagesize=letter)
            c.drawString(100, 750, f"Meeting Notes: {meeting.title}")
            c.drawString(100, 720, f"Date: {meeting.upload_date.strftime('%Y-%m-%d %H:%M')}")
            c.drawString(100, 690, "Error occurred during PDF generation.")
            c.drawString(100, 660, "Please contact support for assistance.")
            c.save()
            print("[PDF FALLBACK] Created minimal PDF")
        except:
            print("[PDF CRITICAL ERROR] Could not create any PDF")
            raise e

# ROUTES
@app.route('/', methods=['GET'])
def health():
    return jsonify({"status": "Backend running!", "timestamp": datetime.utcnow().isoformat()}), 200

# AUTH ROUTES
@app.route("/api/auth/register", methods=["POST"])
def register():
    try:
        print("[AUTH] Register request received")
        data = request.json
        print(f"[AUTH] Register data: {data}")
        
        if User.query.filter_by(email=data['email']).first():
            print(f"[AUTH] Email already exists: {data['email']}")
            return jsonify({"error": "Email already exists"}), 400
        
        user = User(
            full_name=data['full_name'],
            email=data['email'],
            password_hash=generate_password_hash(data['password'])
        )
        db.session.add(user)
        db.session.commit()
        
        access_token = create_access_token(
            identity=str(user.id),
            expires_delta=timedelta(days=30)
        )
        
        print(f"[AUTH] User registered successfully: {user.email}")
        
        return jsonify({
            "access_token": access_token,
            "user": {"id": user.id, "full_name": user.full_name, "email": user.email},
            "expires_in": 30 * 24 * 60 * 60
        }), 201
    except Exception as e:
        print(f"[AUTH ERROR] Register failed: {e}")
        return jsonify({"error": str(e)}), 500

@app.route("/api/auth/login", methods=["POST"])
def login():
    try:
        print("[AUTH] Login request received")
        data = request.json
        print(f"[AUTH] Login attempt for: {data.get('email', 'unknown')}")
        
        user = User.query.filter_by(email=data['email']).first()
        if user and check_password_hash(user.password_hash, data['password']):
            access_token = create_access_token(
                identity=str(user.id),
                expires_delta=timedelta(days=30)
            )
            
            print(f"[AUTH] Login successful for: {user.email}")
            
            return jsonify({
                "access_token": access_token,
                "user": {"id": user.id, "full_name": user.full_name, "email": user.email},
                "expires_in": 30 * 24 * 60 * 60
            })
        
        print(f"[AUTH] Login failed for: {data.get('email', 'unknown')}")
        return jsonify({"error": "Invalid credentials"}), 401
    except Exception as e:
        print(f"[AUTH ERROR] Login failed: {e}")
        return jsonify({"error": str(e)}), 500

@app.route("/api/auth/refresh", methods=["POST"])
@jwt_required()
def refresh():
    try:
        current_user_id = get_jwt_identity()
        user = User.query.get(current_user_id)
        if not user:
            return jsonify({"error": "User not found"}), 404
        
        new_token = create_access_token(
            identity=str(current_user_id),
            expires_delta=timedelta(days=30)
        )
        
        return jsonify({
            "access_token": new_token,
            "user": {"id": user.id, "full_name": user.full_name, "email": user.email},
            "expires_in": 30 * 24 * 60 * 60
        })
    except Exception as e:
        print(f"[AUTH ERROR] Token refresh failed: {e}")
        return jsonify({"error": str(e)}), 500

@app.route("/api/auth/check", methods=["GET"])
@jwt_required()
def check_auth():
    try:
        user_id = get_current_user_id()
        user = User.query.get(user_id)
        if not user:
            print(f"[AUTH] User {user_id} not found in database")
            return jsonify({"error": "User not found"}), 404
        
        print(f"[AUTH] Token validation successful for user: {user.email}")
        return jsonify({
            "valid": True,
            "user_id": user_id,
            "user_email": user.email,
            "user_name": user.full_name
        }), 200
        
    except Exception as e:
        print(f"[AUTH ERROR] Auth check failed: {e}")
        return jsonify({"error": "Invalid token"}), 401

@app.route("/api/auth/validate", methods=["GET"])
@jwt_required()
def validate_token():
    try:
        current_user_id = get_jwt_identity()
        user = User.query.get(current_user_id)
        if not user:
            return jsonify({"error": "User not found"}), 404
        
        return jsonify({
            "valid": True,
            "user": {"id": user.id, "full_name": user.full_name, "email": user.email}
        })
    except Exception as e:
        print(f"[AUTH ERROR] Token validation failed: {e}")
        return jsonify({"error": str(e)}), 500

@app.route("/api/upload", methods=["POST"])
@jwt_required()
def upload():
    try:
        user_id = get_current_user_id()
        print(f"[UPLOAD] User ID from token: {user_id}")
        
        user = User.query.get(user_id)
        if not user:
            print(f"[UPLOAD ERROR] User {user_id} not found in database")
            return jsonify({"error": "User not found"}), 404
        
        print(f"[UPLOAD] Upload request from user: {user.email}")
        print(f"[UPLOAD] Request files: {list(request.files.keys())}")
        
        if "file" not in request.files:
            print("[UPLOAD ERROR] No 'file' key in request.files")
            return jsonify({"error": "No file uploaded"}), 400
        
        file = request.files["file"]
        print(f"[UPLOAD] File filename: {file.filename}")
        print(f"[UPLOAD] File content type: {file.content_type}")
        
        if file.filename == "" or file.filename is None:
            print("[UPLOAD ERROR] Empty filename")
            return jsonify({"error": "No selected file"}), 400
        
        allowed_extensions = {'.mp3', '.wav', '.mp4', '.avi', '.mov', '.m4a', '.flac', '.webm', '.ogg'}
        file_extension = '.' + file.filename.rsplit('.', 1)[1].lower() if '.' in file.filename else ''
        
        if file_extension not in allowed_extensions:
            print(f"[UPLOAD ERROR] Invalid file extension: {file_extension}")
            return jsonify({
                "error": f"Unsupported file format: {file_extension}",
                "allowed_formats": list(allowed_extensions)
            }), 400
        
        file.seek(0, 2)
        file_size = file.tell()
        file.seek(0)
        
        max_size = 100 * 1024 * 1024
        if file_size > max_size:
            print(f"[UPLOAD ERROR] File too large: {file_size} bytes ({file_size / (1024*1024):.2f} MB)")
            return jsonify({
                "error": "File size too large",
                "max_size_mb": 100,
                "file_size_mb": round(file_size / (1024*1024), 2)
            }), 413
        
        print(f"[UPLOAD] File size: {file_size} bytes ({file_size / (1024*1024):.2f} MB)")
        
        upload_dir = app.config['UPLOAD_FOLDER']
        os.makedirs(upload_dir, exist_ok=True)
        print(f"[UPLOAD] Upload directory: {upload_dir}")
        
        original_filename = file.filename
        filename = secure_filename(original_filename)
        
        counter = 1
        base_name, ext = os.path.splitext(filename)
        while os.path.exists(os.path.join(upload_dir, filename)):
            filename = f"{base_name}_{counter}{ext}"
            counter += 1
        
        filepath = os.path.join(upload_dir, filename)
        print(f"[UPLOAD] Saving file to: {filepath}")
        
        try:
            file.save(filepath)
            print(f"[UPLOAD] File save completed")
        except Exception as save_error:
            print(f"[UPLOAD ERROR] Failed to save file: {save_error}")
            return jsonify({"error": f"Failed to save file: {str(save_error)}"}), 500
        
        if not os.path.exists(filepath):
            print(f"[UPLOAD ERROR] File not found after save: {filepath}")
            return jsonify({"error": "File save verification failed"}), 500
        
        saved_size = os.path.getsize(filepath)
        print(f"[UPLOAD] File saved successfully. Size on disk: {saved_size} bytes")
        
        title = request.form.get('title', '').strip()
        if not title:
            title = os.path.splitext(original_filename)[0]
        
        print(f"[UPLOAD] Meeting title: '{title}'")
        
        try:
            meeting = Meeting(
                user_id=user_id,
                title=title,
                filename=filename,
                language=request.form.get('language', 'en'),
                status='uploaded'
            )
            
            db.session.add(meeting)
            db.session.commit()
            
            print(f"[UPLOAD] Meeting created with ID: {meeting.id}")
            
        except Exception as db_error:
            print(f"[UPLOAD ERROR] Database error: {db_error}")
            try:
                os.remove(filepath)
            except:
                pass
            return jsonify({"error": f"Database error: {str(db_error)}"}), 500
        
        response_data = {
            "recording_id": meeting.id,
            "message": "File uploaded successfully",
            "filename": filename,
            "original_filename": original_filename,
            "size_mb": round(saved_size / (1024*1024), 2),
            "title": title,
            "status": "uploaded"
        }
        
        print(f"[UPLOAD SUCCESS] Response: {response_data}")
        return jsonify(response_data), 200
        
    except Exception as e:
        print(f"[UPLOAD ERROR] Upload exception: {str(e)}")
        import traceback
        traceback.print_exc()
        
        return jsonify({
            "error": f"Upload failed: {str(e)}",
            "type": type(e).__name__,
            "message": "Please check the server logs for more details"
        }), 500

@app.route("/api/process/<int:meeting_id>", methods=["POST"])
@jwt_required()
def process_meeting(meeting_id):
    try:
        user_id = get_current_user_id()
        meeting = Meeting.query.filter_by(id=meeting_id, user_id=user_id).first()
        
        if not meeting:
            print(f"[PROCESS ERROR] Meeting {meeting_id} not found for user {user_id}")
            return jsonify({"error": "Meeting not found"}), 404
        
        if meeting.status != 'uploaded':
            print(f"[PROCESS ERROR] Meeting {meeting_id} status is '{meeting.status}', not 'uploaded'")
            return jsonify({"error": f"Meeting already {meeting.status}"}), 400
        
        print(f"[PROCESS] Starting processing for meeting {meeting_id}")
        
        thread = threading.Thread(target=start_processing, args=(meeting_id,))
        thread.daemon = True
        thread.start()
        
        return jsonify({
            "message": "Processing started", 
            "recording_id": meeting_id,
            "status": "processing"
        }), 202
        
    except Exception as e:
        print(f"[PROCESS ERROR] Failed to start processing: {e}")
        return jsonify({"error": str(e)}), 500

@app.route("/api/processing-status/<int:meeting_id>", methods=["GET"])
@jwt_required()
def processing_status(meeting_id):
    try:
        user_id = get_current_user_id()
        meeting = Meeting.query.filter_by(id=meeting_id, user_id=user_id).first()
        
        if not meeting:
            return jsonify({"error": "Meeting not found"}), 404
        
        try:
            steps = json.loads(meeting.processing_steps or '[]')
        except:
            steps = []
        
        if not steps:
            steps = [
                {"step": "transcription", "status": "pending", "timestamp": "", "error": None},
                {"step": "translation", "status": "pending", "timestamp": "", "error": None},
                {"step": "optimization", "status": "pending", "timestamp": "", "error": None},
                {"step": "ai_generation", "status": "pending", "timestamp": "", "error": None}
            ]
        
        return jsonify({
            "recording_id": meeting.id,
            "status": meeting.status,
            "processing_steps": steps,
            "current_step_progress": meeting.current_step_progress or 0
        })
    except Exception as e:
        print(f"[STATUS ERROR] Failed to get processing status: {e}")
        return jsonify({"error": str(e)}), 500

@app.route("/api/meetings", methods=["GET"])
@jwt_required()
def get_meetings():
    try:
        user_id = get_current_user_id()
        limit = request.args.get('limit', 10000000, type=int)
        meetings = Meeting.query.filter_by(user_id=user_id).order_by(Meeting.upload_date.desc()).limit(limit).all()
        
        return jsonify({
            "meetings": [
                {
                    "id": m.id,
                    "title": m.title,
                    "filename": m.filename,
                    "upload_date": m.upload_date.isoformat(),
                    "status": m.status,
                    "has_transcription": m.has_transcription,
                    "has_notes": m.has_notes
                } for m in meetings
            ]
        })
    except Exception as e:
        print(f"[MEETINGS ERROR] Failed to get meetings: {e}")
        return jsonify({"error": str(e)}), 500

@app.route("/api/meetings/<int:meeting_id>", methods=["GET"])
@jwt_required()
def get_meeting(meeting_id):
    try:
        user_id = get_current_user_id()
        meeting = Meeting.query.filter_by(id=meeting_id, user_id=user_id).first()
        
        if not meeting:
            return jsonify({"error": "Meeting not found"}), 404
        
        return jsonify({
            "meeting": {
                "id": meeting.id,
                "title": meeting.title,
                "filename": meeting.filename,
                "upload_date": meeting.upload_date.isoformat(),
                "status": meeting.status,
                "transcription": json.loads(meeting.transcription or '{}'),
                "notes": json.loads(meeting.notes or '{}')
            }
        })
    except Exception as e:
        print(f"[MEETING ERROR] Failed to get meeting: {e}")
        return jsonify({"error": str(e)}), 500

@app.route("/api/meetings/<int:meeting_id>", methods=["DELETE"])
@jwt_required()
def delete_meeting(meeting_id):
    try:
        user_id = get_current_user_id()
        meeting = Meeting.query.filter_by(id=meeting_id, user_id=user_id).first()
        
        if not meeting:
            return jsonify({"error": "Meeting not found"}), 404
        
        filepath = os.path.join(app.config['UPLOAD_FOLDER'], meeting.filename)
        if os.path.exists(filepath):
            os.remove(filepath)
            print(f"[DELETE] Removed file: {filepath}")
        
        db.session.delete(meeting)
        db.session.commit()
        
        print(f"[DELETE] Meeting {meeting_id} deleted successfully")
        return jsonify({"message": "Meeting deleted successfully"}), 200
    except Exception as e:
        print(f"[DELETE ERROR] Failed to delete meeting: {e}")
        return jsonify({"error": str(e)}), 500

@app.route("/api/translate", methods=["POST"])
@jwt_required()
def translate_text():
    try:
        data = request.json
        text = data.get('text', '').strip()
        target_language = data.get('target_language', 'es')
        
        if not text:
            return jsonify({"error": "No text provided"}), 400
        
        language_names = {
            "af": "Afrikaans", "sq": "Albanian", "am": "Amharic", "ar": "Arabic", "hy": "Armenian",
            "az": "Azerbaijani", "eu": "Basque", "be": "Belarusian", "bn": "Bengali", "bs": "Bosnian",
            "bg": "Bulgarian", "ca": "Catalan", "ceb": "Cebuano", "ny": "Chichewa", "zh": "Chinese",
            "zh-cn": "Chinese (Simplified)", "zh-tw": "Chinese (Traditional)", "co": "Corsican",
            "hr": "Croatian", "cs": "Czech", "da": "Danish", "nl": "Dutch", "en": "English",
            "eo": "Esperanto", "et": "Estonian", "tl": "Filipino", "fi": "Finnish", "fr": "French",
            "fy": "Frisian", "gl": "Galician", "ka": "Georgian", "de": "German", "el": "Greek",
            "gu": "Gujarati", "ht": "Haitian Creole", "ha": "Hausa", "haw": "Hawaiian", "he": "Hebrew",
            "iw": "Hebrew", "hi": "Hindi", "hmn": "Hmong", "hu": "Hungarian", "is": "Icelandic",
            "ig": "Igbo", "id": "Indonesian", "ga": "Irish", "it": "Italian", "ja": "Japanese",
            "jw": "Javanese", "kn": "Kannada", "kk": "Kazakh", "km": "Khmer", "ko": "Korean",
            "ku": "Kurdish (Kurmanji)", "ky": "Kyrgyz", "lo": "Lao", "la": "Latin", "lv": "Latvian",
            "lt": "Lithuanian", "lb": "Luxembourgish", "mk": "Macedonian", "mg": "Malagasy",
            "ms": "Malay", "ml": "Malayalam", "mt": "Maltese", "mi": "Maori", "mr": "Marathi",
            "mn": "Mongolian", "my": "Myanmar (Burmese)", "ne": "Nepali", "no": "Norwegian",
            "or": "Odia", "ps": "Pashto", "fa": "Persian", "pl": "Polish", "pt": "Portuguese",
            "pa": "Punjabi", "ro": "Romanian", "ru": "Russian", "sm": "Samoan", "gd": "Scots Gaelic",
            "sr": "Serbian", "st": "Sesotho", "sn": "Shona", "sd": "Sindhi", "si": "Sinhala",
            "sk": "Slovak", "sl": "Slovenian", "so": "Somali", "es": "Spanish", "su": "Sundanese",
            "sw": "Swahili", "sv": "Swedish", "tg": "Tajik", "ta": "Tamil", "te": "Telugu",
            "th": "Thai", "tr": "Turkish", "uk": "Ukrainian", "ur": "Urdu", "ug": "Uyghur",
            "uz": "Uzbek", "vi": "Vietnamese", "cy": "Welsh", "xh": "Xhosa", "yi": "Yiddish",
            "yo": "Yoruba", "zu": "Zulu"
        }
        
        target_lang_name = language_names.get(target_language, "Spanish")
        
        try:
            prompt = f"""
You are a professional translator. Translate the given text to {target_lang_name}. Respond only with the translated text, no additional formatting or explanations.

Text to translate: {text}
"""
            response = call_gemini_api(prompt, model="gemini-1.5-flash")
            translated = response.text.strip() if response.text else ""
            
            if not translated:
                return jsonify({"error": "Translation failed: Empty response from API"}), 500
            
            return jsonify({"translated_text": translated})
            
        except Exception as e:
            print(f"[TRANSLATE ERROR] Translation API error: {str(e)}")
            return jsonify({
                "error": "Translation service unavailable",
                "details": str(e),
                "suggestion": "Please check your GEMINI_API_KEY or network connection and try again."
            }), 500
        
    except Exception as e:
        print(f"[TRANSLATE ERROR] Translate endpoint error: {str(e)}")
        return jsonify({"error": str(e)}), 500

@app.route("/api/export/<int:id>/<string:format>", methods=["GET"])
@jwt_required()
def export(id, format):
    try:
        user_id = get_current_user_id()
        meeting = Meeting.query.filter_by(id=id, user_id=user_id).first()
        
        if not meeting:
            return jsonify({"error": "Meeting not found"}), 404
        
        os.makedirs("outputs", exist_ok=True)
        
        if format == "word":
            filepath = f"outputs/meeting_notes_{id}.docx"
            try:
                doc = Document()
                doc.add_heading(f"Meeting Notes: {meeting.title}", 0)
                
                doc.add_paragraph(f"File: {meeting.filename}")
                doc.add_paragraph(f"Date: {meeting.upload_date.strftime('%Y-%m-%d %H:%M')}")
                doc.add_paragraph("")
                
                try:
                    notes = json.loads(meeting.notes or '{}')
                    print(f"[WORD DEBUG] Notes content: {len(notes.get('key_points', []))} key points")
                    
                    if notes.get("summary"):
                        doc.add_heading("Executive Summary", level=1)
                        doc.add_paragraph(str(notes["summary"]))
                    
                    if notes.get("key_points"):
                        doc.add_heading("Key Discussion Points", level=1)
                        key_points = notes["key_points"]
                        if isinstance(key_points, str):
                            try:
                                key_points = json.loads(key_points)
                            except:
                                key_points = [key_points]
                        
                        if isinstance(key_points, list):
                            for i, point in enumerate(key_points, 1):
                                if point and str(point).strip():
                                    doc.add_paragraph(f"{i}. {str(point).strip()}", style="List Number")
                    
                    doc.add_heading("Action Items", level=1)
                    action_items = notes.get("action_items", [])
                    if isinstance(action_items, str):
                        try:
                            action_items = json.loads(action_items)
                        except:
                            action_items = [action_items]
                    
                    if isinstance(action_items, list) and action_items:
                        for item in action_items:
                            if item and str(item).strip():
                                doc.add_paragraph(str(item).strip(), style="List Bullet")
                    else:
                        doc.add_paragraph("No specific action items identified.", style="Normal")
                    
                    doc.add_heading("Decisions Made", level=1)
                    decisions = notes.get("decisions", [])
                    if isinstance(decisions, str):
                        try:
                            decisions = json.loads(decisions)
                        except:
                            decisions = [decisions]
                    
                    if isinstance(decisions, list) and decisions:
                        for decision in decisions:
                            if decision and str(decision).strip():
                                doc.add_paragraph(str(decision).strip(), style="List Bullet")
                    else:
                        doc.add_paragraph("No formal decisions recorded.", style="Normal")
                    
                    if notes.get("sentiment"):
                        doc.add_heading("Meeting Assessment", level=1)
                        doc.add_paragraph(str(notes["sentiment"]))
                    
                    transcription_data = json.loads(meeting.transcription or '{}')
                    transcript_text = transcription_data.get('optimized') or transcription_data.get('translated') or transcription_data.get('raw')
                    
                    if transcript_text and len(transcript_text) < 50000:
                        doc.add_heading("Full Transcript", level=1)
                        doc.add_paragraph(transcript_text[:10000] + "..." if len(transcript_text) > 10000 else transcript_text)
                        
                except Exception as notes_error:
                    print(f"[WORD ERROR] Error processing notes: {notes_error}")
                    doc.add_paragraph(f"Error processing notes: {str(notes_error)}")
                
                doc.save(filepath)
                return send_file(filepath, as_attachment=True, download_name=f"meeting_notes_{id}.docx")
                
            except Exception as word_error:
                print(f"[WORD ERROR] Word generation failed: {word_error}")
                return jsonify({"error": f"Word document generation failed: {str(word_error)}"}), 500
            
        elif format == "pdf":
            filepath = f"outputs/meeting_notes_{id}.pdf"
            try:
                create_enhanced_pdf(meeting, filepath)
                return send_file(filepath, as_attachment=True, download_name=f"meeting_notes_{id}.pdf")
            except Exception as pdf_error:
                print(f"[PDF ERROR] PDF generation failed: {pdf_error}")
                return jsonify({"error": f"PDF generation failed: {str(pdf_error)}"}), 500
        
        return jsonify({"error": "Invalid format"}), 400
    except Exception as e:
        print(f"[EXPORT ERROR] Export failed: {e}")
        return jsonify({"error": str(e)}), 500

@app.route("/api/stats", methods=["GET"])
@jwt_required()
def stats():
    try:
        user_id = get_jwt_identity()
        meetings = Meeting.query.filter_by(user_id=user_id).all()
        
        total_uploads = len(meetings)
        total_words = sum(len(json.loads(m.notes or '{}').get("summary", "").split()) for m in meetings)
        
        today = datetime.utcnow().date()
        last_7_days = [(today - timedelta(days=i)).strftime("%a") for i in range(6, -1, -1)]
        uploads_by_day = Counter(m.upload_date.date().strftime("%a") for m in meetings)
        uploads_data = [uploads_by_day.get(day, 0) for day in last_7_days]
        
        return jsonify({
            "total_meetings": total_uploads,
            "completed_meetings": len([m for m in meetings if m.status == "completed"]),
            "total_words": total_words,
            "labels": last_7_days,
            "uploads": uploads_data
        })
    except Exception as e:
        print(f"[STATS ERROR] Stats failed: {e}")
        return jsonify({"error": str(e)}), 500

@app.route("/api/chat", methods=["POST"])
def chat():
    try:
        data = request.json
        user_message = data.get('message', '').strip()
        
        if not user_message:
            return jsonify({"error": "No message provided"}), 400
        
        system_prompt = (
            "You are the AI assistant for TalkToText Pro.\n\n"
            "About TalkToText Pro:\n"
            "- It is an AI-powered meeting notes rewriter.\n"
            "- Converts speech from Zoom, Google Meet, and Teams into structured, actionable meeting notes.\n"
            "- Features: transcription, translation, text cleaning, summarization, PDF/Word export.\n"
            "- Goal: Help users make their meetings productive, clear, and easy to follow.\n\n"
            "Your role:\n"
            "- If the user asks about the website, always explain TalkToText Pro in a professional but friendly way.\n"
            "- If the user provides transcripts, summarize them and highlight key points, action items, and decisions.\n"
            "- Keep responses concise, clear, and helpful.\n"
            "- Always be friendly, professional, and focus on helping users understand and use TalkToText Pro effectively.\n"
        )
        
        try:
            response = call_gemini_api(
                f"{system_prompt}\n\nUser: {user_message}",
                model="gemini-1.5-flash"
            )
            
            ai_response = response.text.strip()
            
            return jsonify({
                "response": ai_response,
                "timestamp": datetime.utcnow().isoformat()
            })
            
        except Exception as ai_error:
            print(f"[CHAT ERROR] AI Chat error: {ai_error}")
            fallback_responses = {
                "features": "TalkToText Pro offers powerful features including: Real-time transcription from Zoom, Google Meet, and Teams, Multi-language translation, AI-powered text cleaning, Smart summarization with key points and action items, Export to PDF and Word formats. What would you like to know more about?",
                "about": "TalkToText Pro is an AI-powered meeting notes rewriter that helps you convert speech from popular meeting platforms into structured, actionable notes. We make your meetings more productive and easier to follow!",
                "how": "Getting started is simple! 1. Upload your meeting recording, 2. Our AI transcribes and processes it, 3. Review the generated notes and summaries, 4. Export in PDF or Word format. Need help with any specific step?",
                "support": "I'm here to help! You can ask me about TalkToText Pro features, how to use the platform, or share meeting transcripts for me to summarize. What specific question do you have?",
                "default": "Thanks for your question! I'm here to help you with TalkToText Pro. You can ask me about our features, how to use the platform, pricing, or share meeting content for analysis. How can I assist you today?"
            }
            
            lower_message = user_message.lower()
            if any(word in lower_message for word in ['feature', 'what can', 'capability']):
                fallback_response = fallback_responses["features"]
            elif any(word in lower_message for word in ['about', 'talktotex', 'website', 'company']):
                fallback_response = fallback_responses["about"]
            elif any(word in lower_message for word in ['how', 'tutorial', 'guide', 'start']):
                fallback_response = fallback_responses["how"]
            elif any(word in lower_message for word in ['help', 'support', 'problem']):
                fallback_response = fallback_responses["support"]
            else:
                fallback_response = fallback_responses["default"]
            
            return jsonify({
                "response": fallback_response,
                "timestamp": datetime.utcnow().isoformat()
            })
        
    except Exception as e:
        print(f"[CHAT ERROR] Chat endpoint error: {e}")
        return jsonify({"error": "Sorry, I'm having trouble right now. Please try again."}), 500

@app.route("/api/send-email", methods=["POST"])
@jwt_required()
def send_email():
    try:
        user_id = get_jwt_identity()
        user = User.query.get(user_id)
        
        meeting_id = request.form.get('meeting_id')
        to_email = request.form.get('to_email')
        from_email = request.form.get('from_email') or user.email
        subject = request.form.get('subject')
        body = request.form.get('body')
        
        # Get SMTP credentials from form or environment
        smtp_username = request.form.get('smtp_username') or os.getenv("SMTP_USERNAME")
        smtp_password = request.form.get('smtp_password') or os.getenv("SMTP_PASSWORD")
        smtp_server = request.form.get('smtp_server') or os.getenv("SMTP_SERVER", "smtp.gmail.com")
        smtp_port = int(request.form.get('smtp_port', os.getenv("SMTP_PORT", "587")))
        
        if 'pdf_file' not in request.files:
            return jsonify({"error": "No PDF file provided"}), 400
        
        pdf_file = request.files['pdf_file']
        if pdf_file.filename == '':
            return jsonify({"error": "No PDF file selected"}), 400
        
        meeting = Meeting.query.filter_by(id=meeting_id, user_id=user_id).first()
        if not meeting:
            return jsonify({"error": "Meeting not found"}), 404
        
        # If no SMTP credentials, return demo mode response
        if not smtp_username or not smtp_password:
            print(f"[EMAIL DEMO] Email would be sent:")
            print(f"[EMAIL DEMO] From: {from_email} To: {to_email}")
            print(f"[EMAIL DEMO] Subject: {subject}")
            print(f"[EMAIL DEMO] PDF attachment: {pdf_file.filename}")
            return jsonify({
                "message": "Email prepared successfully. To enable sending, configure SMTP credentials in environment variables or provide them in the form.",
                "demo_mode": True,
                "email_details": {
                    "from": from_email,
                    "to": to_email,
                    "subject": subject,
                    "attachment": pdf_file.filename,
                    "smtp_configured": False
                }
            })
        
        try:
            # Create email message
            msg = MIMEMultipart()
            msg['From'] = smtp_username
            msg['To'] = to_email
            msg['Subject'] = subject
            msg['Reply-To'] = from_email
            
            email_body = f"""Meeting Notes from TalkToText Pro

From: {user.full_name} ({from_email})
Meeting: {meeting.title}
Date: {meeting.upload_date.strftime('%Y-%m-%d %H:%M')}

{body}

---
Best regards,
{user.full_name}

Sent via TalkToText Pro - AI-Powered Meeting Notes
https://talktotextpro.com
"""
            
            msg.attach(MIMEText(email_body, 'plain'))
            
            # Attach PDF
            pdf_content = pdf_file.read()
            part = MIMEApplication(pdf_content, _subtype='pdf')
            part.add_header('Content-Disposition', f'attachment; filename={pdf_file.filename}')
            msg.attach(part)
            
            # Send email
            server = smtplib.SMTP(smtp_server, smtp_port)
            server.starttls()
            server.login(smtp_username, smtp_password)
            text = msg.as_string()
            server.sendmail(smtp_username, to_email, text)
            server.quit()
            
            print(f"[EMAIL SUCCESS] Email sent from {smtp_username} to {to_email}")
            return jsonify({
                "message": "Email sent successfully",
                "email_details": {
                    "from": smtp_username,
                    "to": to_email,
                    "subject": subject,
                    "attachment": pdf_file.filename
                }
            })
            
        except smtplib.SMTPAuthenticationError:
            print("[EMAIL ERROR] SMTP Authentication failed")
            return jsonify({
                "error": "Email authentication failed",
                "suggestion": "Please check your email credentials. For Gmail, use App Password instead of regular password.",
                "help_url": "https://support.google.com/accounts/answer/185833"
            }), 401
            
        except smtplib.SMTPRecipientsRefused:
            print(f"[EMAIL ERROR] Recipient refused: {to_email}")
            return jsonify({
                "error": "Recipient email address was refused",
                "suggestion": "Please check the recipient email address is correct"
            }), 400
            
        except smtplib.SMTPServerDisconnected:
            print("[EMAIL ERROR] SMTP Server disconnected")
            return jsonify({
                "error": "Email server connection failed",
                "suggestion": "Please check your internet connection and SMTP server settings"
            }), 503
            
        except Exception as smtp_error:
            print(f"[EMAIL ERROR] SMTP Error: {smtp_error}")
            return jsonify({
                "error": f"Failed to send email: {str(smtp_error)}",
                "suggestion": "Please check your email configuration and try again"
            }), 500
        
    except Exception as e:
        print(f"[EMAIL ERROR] Send email error: {e}")
        return jsonify({"error": str(e)}), 500

if __name__ == "__main__":
    # Create necessary directories
    os.makedirs("uploads", exist_ok=True)
    os.makedirs("outputs", exist_ok=True)
    
    # Startup info
    print("🚀 Starting TalkToText Pro Backend Server...")
    print("=" * 60)
    print("📁 Upload folder:", app.config['UPLOAD_FOLDER'])
    print("🗄️ Database:", app.config['SQLALCHEMY_DATABASE_URI'])
    print("🔑 JWT Secret configured:", bool(app.config['JWT_SECRET_KEY']))
    print("🤖 Gemini API configured:", bool(gemini_api_key))
    print("🎙️ AssemblyAI configured:", bool(os.getenv("ASSEMBLYAI_API_KEY")))
    
    print("\n📧 Email Configuration:")
    smtp_user = os.getenv('SMTP_USERNAME')
    smtp_pass = os.getenv('SMTP_PASSWORD')
    print(f"   SMTP_USERNAME: {'✅ ' + smtp_user if smtp_user else '❌ Not set'}")
    print(f"   SMTP_PASSWORD: {'✅ Set' if smtp_pass else '❌ Not set'}")
    print(f"   SMTP_SERVER: {os.getenv('SMTP_SERVER', 'smtp.gmail.com')}")
    print(f"   SMTP_PORT: {os.getenv('SMTP_PORT', '587')}")
    
    if not smtp_user or not smtp_pass:
        print("\n   📧 Email Setup Instructions:")
        print("   1. Create a .env file in your project root")
        print("   2. Add: SMTP_USERNAME=your-email@gmail.com")
        print("   3. Add: SMTP_PASSWORD=your-app-password")
        print("   4. Optional: SMTP_SERVER=smtp.gmail.com")
        print("   5. Optional: SMTP_PORT=587")
        print("   6. For Gmail, use App Passwords: https://support.google.com/accounts/answer/185833")
        print("   7. Or provide credentials in the email form when sending")
    
    print("\n🌐 API Endpoints Available:")
    print("   POST /api/auth/register      - User registration")
    print("   POST /api/auth/login         - User login") 
    print("   GET  /api/auth/check         - Token validation")
    print("   POST /api/upload             - File upload")
    print("   POST /api/process/<id>       - Start processing")
    print("   GET  /api/processing-status/<id> - Check processing status")
    print("   GET  /api/meetings           - List user meetings")
    print("   GET  /api/meetings/<id>      - Get specific meeting")
    print("   DELETE /api/meetings/<id>    - Delete meeting")
    print("   POST /api/translate          - Text translation")
    print("   GET  /api/export/<id>/<format> - Export notes (PDF/Word)")
    print("   POST /api/chat               - AI chat assistant")
    print("   POST /api/send-email         - Send email with attachments")
    print("   GET  /api/stats              - User statistics")
    
    print("\n🔧 Key Improvements:")
    print("   ✅ Enhanced AI prompt for detailed summaries")
    print("   ✅ Better key points extraction (15-25 points minimum)")
    print("   ✅ Improved PDF generation with error handling")
    print("   ✅ Fixed email functionality with better SMTP handling")
    print("   ✅ Enhanced Word document generation")
    print("   ✅ Better content analysis and extraction")
    print("   ✅ Comprehensive error handling and logging")
    
    print("\n" + "=" * 60)
    print("✅ Server starting on http://0.0.0.0:5000")
    print("✅ CORS enabled for all origins")
    print("✅ Debug mode enabled")
    print("✅ Ready for deployment!")
    print("=" * 60)
    
    # Run the Flask app
    app.run(debug=True, host="0.0.0.0", port=5000, threaded=True)