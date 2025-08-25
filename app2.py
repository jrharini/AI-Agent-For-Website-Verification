import re
import time
from flask import Flask, render_template, request
from bs4 import BeautifulSoup
import language_tool_python
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.common.by import By
from langchain.chat_models import ChatOllama
from langchain.schema import HumanMessage, SystemMessage
from urllib.parse import urljoin, urlparse
import subprocess
import os
import textstat
import json

app = Flask(__name__)
search_history = []
copy_audit_result_global = {}  # Global to reuse in another route
conversion_audit_result_global = {}  # New global for conversion audit
mobile_usability_result_global = {}
llm = ChatOllama(model="koesn/llama3-8b-instruct:latest")

def extract_lighthouse_score_from_html(json_path):
    try:
        with open(json_path, 'r', encoding='utf-8') as f:
            data = json.load(f)

        # Extract performance score
        if "categories" in data and "performance" in data["categories"]:
            score = data["categories"]["performance"].get("score", 0)
            return int(score * 100)
        else:
            print("❌ 'performance' score not found")
            return 0
    except Exception as e:
        print(f"❌ Failed to extract technical score from JSON: {e}")
        return 0

SYSTEM_PROMPT = """
You are a strict American English spellchecker.
Your job is to do ONLY the following:
1. Identify spelling mistakes.
2. Identify British English spellings and convert them to American English.
✅ Use ONLY the list of words provided — do not add or imagine extra words.
❌ Output ONLY in the format: wrongword -> correctword
❌ Do NOT list correct words.
❌ Do NOT fix brand names, proper nouns, or names unless they are clearly misspelled.
❌ Do NOT explain anything.
"""


def clean_llm_output(output):
    clean_lines = []
    for line in output.splitlines():
        match = re.match(r"^\s*(\w[\w'-]*)\s*->\s*([A-Za-z][A-Za-z\s'-]*)$", line)
        if match:
            wrong, correct = match.group(1), match.group(2)
            if wrong.lower() != correct.lower():
                clean_lines.append(f"{wrong} -> {correct}")
    return "\n".join(clean_lines)

def spellcheck_with_llm(word_list):
    text = ", ".join(word_list)
    prompt = f"Here is a list of words:\n[{text}]\n\nReturn ONLY the misspelled or British English words corrected to American English, in the format:\nwrongword -> correctword"
    response = llm([SystemMessage(content=SYSTEM_PROMPT), HumanMessage(content=prompt)])
    return clean_llm_output(response.content.strip())

def spellcheck_with_dict(text):
    tool = language_tool_python.LanguageTool('en-US')
    matches = tool.check(text)
    corrections = {}
    for match in matches:
        if "Possible spelling mistake" in match.message and match.replacements:
            word = text[match.offset:match.offset + match.errorLength]
            corrections[word] = match.replacements[0]
    return '\n'.join(f"{k} → {v}" for k, v in corrections.items())

def extract_internal_links(soup, base_url):
    links = set()
    def is_valid_text_link(href):
        invalid_exts = (".jpg", ".jpeg", ".png", ".gif", ".svg", ".ico", ".css", ".js", ".pdf", ".zip", ".mp4", ".mp3")
        return not href.lower().endswith(invalid_exts)

    for tag in soup.find_all("a", href=True):
        href = tag['href']
        if not is_valid_text_link(href):
            continue
        full_url = urljoin(base_url, href)
        parsed = urlparse(full_url)
        if parsed.scheme in ("http", "https"):
            links.add(full_url)

    return list(links)

def run_copy_audits(text):
    audits = {}
    headlines = re.findall(r'(?m)^[A-Z][^\n]{0,120}$', text)
    audits["headline_check"] = any(len(line.split()) <= 12 for line in headlines)

    try:
        audits["flesch_score"] = textstat.flesch_kincaid_grade(text)
        audits["is_readable"] = audits["flesch_score"] <= 8
    except:
        audits["flesch_score"] = None
        audits["is_readable"] = False

    cta_verbs = ["get", "start", "book"]
    audits["cta_present"] = any(word.lower() in text.lower() for word in cta_verbs)

    jargon_words = ['leverage', 'synergy', 'stakeholder', 'paradigm']
    total_words = len(text.split())
    jargon_count = sum(text.lower().count(jargon) for jargon in jargon_words)
    audits["jargon_ok"] = (jargon_count / total_words * 100) < 2 if total_words else True

    audits["testimonial_present"] = bool(re.search(r"(testimonial|review)", text, re.I))

    return audits
def run_mobile_usability_audit(soup):
    checks = {}
    checks["viewport_tag"] = bool(soup.find('meta', attrs={'name': 'viewport'}))
    checks["tap_target_ok"] = True  # Example logic: you can add your own rules
    checks["no_horizontal_scroll"] = True  # Example: add checks for overflow-x
    checks["telephone_field_numeric"] = True  # Example: look for input[type="tel"]
    checks["cta_thumb_zone"] = True  # Example: you’d need JS/visual checks

    return checks

def perform_conversion_audit(soup):
    cta_buttons = soup.find_all('a', string=re.compile("(?i)(buy|try|get|start|learn|sign|register)"))
    cta_styles = set(btn.get('style', '') for btn in cta_buttons)
    cta_unique = len(cta_styles) <= 1

    def cta_style_check(tag):
        style = tag.get("style", "")
        return 'font-size: 16px' in style and ('bold' in style or 'font-weight: 700' in style)

    cta_size_ok = any(cta_style_check(tag) for tag in cta_buttons)

    forms = soup.find_all('form')
    form_field_ok = True
    for form in forms:
        inputs = form.find_all(['input', 'select', 'textarea'])
        if len(inputs) > 5:
            form_field_ok = False
            break

    trust_keywords = ['secure', 'verified', 'https', 'badge', 'ssl']
    trust_badges = any(any(k in (img.get('alt', '') + img.get('src', '')).lower() for k in trust_keywords)
                       for img in soup.find_all('img'))

    urgency_phrases = ['only', 'left', 'hurry', 'limited', 'ends soon', 'countdown', 'time left']
    text_content = soup.get_text(separator=' ').lower()
    urgency_present = any(word in text_content for word in urgency_phrases)

    return {
        "cta_unique": cta_unique,
        "cta_size_ok": cta_size_ok,
        "form_field_ok": form_field_ok,
        "trust_badges": trust_badges,
        "urgency_present": urgency_present
    }

def perform_visual_audit(soup):
    result = {}

    # Rule 1: Hero headline ≥ 32px and bold
    result["hero_headline"] = any(
        "font-size" in tag.get("style", "") and "32px" in tag.get("style", "") and
        ("bold" in tag.get("style", "") or "font-weight: 700" in tag.get("style", ""))
        for tag in soup.find_all(re.compile('^h[1-3]$'))
    )

    # Rule 2: CTA contrast placeholder (hardcoded to True or implement real contrast check)
    result["cta_contrast"] = True  # For now, assume passes

    # Rule 3: Only 1 H1 present
    h1_tags = soup.find_all("h1")
    result["single_h1"] = len(h1_tags) == 1

    # Rule 4: Brand palette check (assumes checking <style> or inline)
    colors_used = set(re.findall(r'#(?:[0-9a-fA-F]{3}){1,2}', soup.prettify()))
    result["limited_palette"] = len(colors_used) <= 2

    # Rule 5: CTA + Key message inside initial viewport (simplified)
    result["cta_above_fold"] = True  # Could be refined with JS/viewport detection

    return result

def scrape_main_text(url, selected_mode="combined"):
    options = Options()
    options.add_argument("--headless")
    options.add_argument("--disable-gpu")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--log-level=3")

    driver = webdriver.Chrome(options=options)
    try:
        driver.get(url)
        WebDriverWait(driver, 15).until(EC.presence_of_element_located((By.TAG_NAME, "body")))
        driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
        time.sleep(2)

        soup = BeautifulSoup(driver.page_source, "html.parser")
        base_url = driver.current_url

        for tag in soup(["script", "style", "noscript"]):
            tag.decompose()

        main_text = soup.get_text(separator="\n")
        nested_texts = []
        nested_links = extract_internal_links(soup, base_url)

        for link in nested_links[:1]:
            try:
                driver.get(link)
                WebDriverWait(driver, 10).until(EC.presence_of_element_located((By.TAG_NAME, "body")))
                time.sleep(2)
                nested_soup = BeautifulSoup(driver.page_source, "html.parser")
                for tag in nested_soup(["script", "style", "noscript"]):
                    tag.decompose()
                nested_text = nested_soup.get_text(separator="\n")
                nested_texts.append(nested_text)
            except Exception:
                continue
    finally:
        driver.quit()

    full_text = main_text + "\n".join(nested_texts)
    all_words = re.findall(r'\b[a-zA-Z]{2,}\b', full_text)
    filtered_words = [w for w in all_words if not (w.islower() and len(w) <= 3)]

    word_count = len(filtered_words)
    alpha_text = '\n'.join(filtered_words)
    joined_text = ' '.join(filtered_words)

    if word_count < 20:
        return f"Page too short or empty to process. Word count: {word_count}", word_count, "", ""

    dict_corrections = ""
    llm_corrections = ""

    if selected_mode in ["dictionary", "combined"]:
        dict_corrections = spellcheck_with_dict(joined_text)

    if selected_mode in ["llm", "combined"]:
        llm_corrections = spellcheck_with_llm(filtered_words)

    return alpha_text, word_count, dict_corrections, llm_corrections, soup

def process_paragraph_text(text):
    all_words = re.findall(r'\b[a-zA-Z]{2,}\b', text)
    filtered_words = [w for w in all_words if not (w.islower() and len(w) <= 3)]
    word_count = len(filtered_words)
    alpha_text = '\n'.join(filtered_words)
    joined_text = ' '.join(filtered_words)
    return alpha_text, word_count, joined_text

def audit_website(url, output_dir="static"):
    os.makedirs(output_dir, exist_ok=True)
    json_output_file = os.path.join(output_dir, "wireframe.json")

    lighthouse_cmd = r"C:\\Users\\iamjr\\AppData\\Roaming\\npm\\lighthouse.cmd"

    try:
        subprocess.run([
            lighthouse_cmd,
            url,
            "--quiet",
            "--chrome-flags=--headless",
            f"--output-path={json_output_file}",
            "--output=json"
        ], check=True)
    except Exception as e:
        print("❌ Lighthouse audit failed (JSON):", e)
        return False

    # ✅ Only check existence AFTER audit
    if os.path.exists(json_output_file) and os.path.getsize(json_output_file) > 0:
        print(f"✅ JSON created at {json_output_file}")
        return True
    else:
        print("❌ JSON not created.")
        return False

@app.route("/", methods=["GET", "POST"])
def index():
    global search_history, copy_audit_result_global, conversion_audit_result_global, mobile_usability_result_global

    extracted_text = None
    word_count = 0
    dict_output = ""
    llm_output = ""
    selected_mode = "combined"
    message = ""
    show_audit_report = False
    copy_audit_result = {}
    conversion_audit_result = {}
    mobile_usability_result = {}  # <- local result

    if request.method == "POST":
        input_text = request.form.get("input_text", "").strip()
        if input_text:
            search_history.append(input_text)
            selected_mode = request.form.get("mode", "combined")

        if input_text.startswith("http://") or input_text.startswith("https://"):
            # ✅ scrape main text returns soup
            extracted_text, word_count, dict_output, llm_output, soup = scrape_main_text(input_text, selected_mode)

            # ✅ run copy audits
            copy_audit_result = run_copy_audits(extracted_text)
            copy_audit_result_global = copy_audit_result

            # ✅ run conversion audit
            conversion_audit_result = perform_conversion_audit(soup)
            conversion_audit_result_global = conversion_audit_result

            # ✅ run mobile usability audit HERE
            mobile_usability_result = run_mobile_usability_audit(soup)
            mobile_usability_result_global = mobile_usability_result
            visual_audit_result = perform_visual_audit(soup)
            global visual_audit_result_global
            visual_audit_result_global = visual_audit_result
            audit_website(input_text)
            show_audit_report = True

        else:
            # Non-URL: paragraphs only
            extracted_text, word_count, joined_text = process_paragraph_text(input_text)
            if selected_mode in ["dictionary", "combined"]:
                dict_output = spellcheck_with_dict(joined_text)
            if selected_mode in ["llm", "combined"]:
                llm_output = spellcheck_with_llm(re.findall(r'\b[a-zA-Z]{2,}\b', joined_text))
            copy_audit_result = run_copy_audits(joined_text)
            copy_audit_result_global = copy_audit_result
            # ✅ no soup so no conversion/mobile audit
            show_audit_report = False
            critical_issues = [
    {"title": "Missing alt text on images", "category": "Accessibility"},
    {"title": "Slow page load on mobile", "category": "Technical"},
    {"title": "Unclear value proposition", "category": "Persuasion"},
    {"title": "CTA buttons not prominent", "category": "Conversion"},
]

    return render_template(
        "index2.html",
        text=extracted_text,
        word_count=word_count,
        dict_corrections=dict_output,
        llm_corrections=llm_output,
        selected_mode=selected_mode,
        message=message,
        show_audit_report=show_audit_report,
        copy_audit_result=copy_audit_result,
        conversion_audit_result=conversion_audit_result,
        # ✅ optional: pass it here if you want to display on index too
        mobile_usability_result=mobile_usability_result
    )


@app.route("/copy-audit")
def view_copy_audit():
    input_url = search_history[-1] if search_history else "N/A"

    audit_website(input_url)

    # Wait until the JSON is generated or timeout after 10 seconds
    json_path = "static/wireframe.json"
    for _ in range(20):  # max 10 seconds
        if os.path.exists(json_path) and os.path.getsize(json_path) > 0:
            break
        time.sleep(0.5)  # wait before checking again

    technical_score = extract_lighthouse_score_from_html(json_path)
    print(f"⚙️ Extracted technical score: {technical_score}")
    technical_issues = [{"title": "Eliminate render-blocking resources", "description": "Resources block first paint..."}]

    # Dynamically calculate based on existing global results
    accessibility_issues = [
        {"title": k.replace('_', ' ').title(), "description": f"{'Pass' if v else 'Fail'}"} 
        for k, v in mobile_usability_result_global.items() if not v
    ]
    accessibility_score = 100 - len(accessibility_issues) * 5

    ux_issues = [
        {"title": k.replace('_', ' ').title(), "description": f"{'Pass' if v else 'Fail'}"} 
        for k, v in conversion_audit_result_global.items() if not v
    ]
    conversion_score = 100 - len(ux_issues) * 5

    persuasion_issues = [
        {"title": k.replace('_', ' ').title(), "description": f"{'Pass' if v else 'Fail'}"} 
        for k, v in copy_audit_result_global.items() if not v
    ]
    persuasion_score = 100 - len(persuasion_issues) * 5

    return render_template("wireframe.html",
                           input_url=input_url,
                           technical_score=technical_score,
                           accessibility_score=accessibility_score,
                           conversion_score=conversion_score,
                           persuasion_score=persuasion_score,
                           technical_issues=technical_issues,
                           accessibility_issues=accessibility_issues,
                           ux_issues=ux_issues,
                           persuasion_issues=persuasion_issues)

if __name__ == "__main__":
    app.run(debug=True)