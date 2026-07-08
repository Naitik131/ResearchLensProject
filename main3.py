import stat
import time
from typing import TypedDict, List, Dict, Optional, Annotated
import operator
from langgraph.graph import StateGraph, START, END
from langgraph.checkpoint.memory import InMemorySaver
import fitz
import requests
from langgraph.checkpoint.sqlite import SqliteSaver
from langchain_core.messages import (
    HumanMessage,
    AIMessage,
    SystemMessage
)
from IPython.display import Image, display
from datetime import datetime



from langchain_core.prompts import ChatPromptTemplate
from langchain_google_genai import ChatGoogleGenerativeAI
import os
from dotenv import load_dotenv
from langgraph.constants import Send

load_dotenv()
import threading

gemini_semaphore = threading.Semaphore(5)



current_year = datetime.now().year

min_year = current_year - 5



model = ChatGoogleGenerativeAI(model = 'gemini-3.1-flash-lite')

class state(TypedDict):
    user_query:str
    search_required:bool
    search_queries:list[str]
    papers:list
    pdf_paths:list[str]
    paper_contents:list[str]
    paper_summaries:list
    downloaded_ids: list[str]
    task:dict
    search_round: int
    review_plan:list
    worker_outputs: Annotated[list, operator.add]
    synthesis: str

    final_ans:str
    



from pydantic import BaseModel, Field

class QueryClassifierOutput(BaseModel):

    search_required: bool = Field(
        description="Whether literature review search is required"
    )

    search_queries: list[str] = Field(
        description="Academic search queries for Semantic Scholar"
    )


class ResearchPapers(BaseModel):

    title: str
    abstract: str  | None = None
    year: int  | None = None
    url: str  | None = None
    citation_count:int  | None = None
    is_oa:str | None = None
    oa_url :str | None = None
    pdf_url: str | None = None






def query_classifier_node (state):
    user_query = state['user_query']
    response = model.with_structured_output(QueryClassifierOutput).invoke(
        
        f"""
    Classify the user query.

    Return:
    - search_required = True if the query needs a literature review,
      survey, research papers, state-of-the-art analysis,
      research gaps, datasets, or academic comparison.

    - search_required = False for normal chat.

    If search_required is True:
    Generate 4-5 high-signal academic search queries suitable for
    Semantic Scholar.

    The queries should cover:
    - core topic
    - review papers
    - recent methods
    - benchmarks
    - datasets

    If search_required is False:
    Return an empty query list.

    User Query:
    {state["user_query"]}
    """
)

    return {
    "search_required": response.search_required,
    "search_queries": response.search_queries
    }




import requests
import os
import time


def download_pdf(pdf_url, save_path, retries=2, backoff=3):

    headers = {
        'User-Agent': (
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
            'AppleWebKit/537.36 (KHTML, like Gecko) '
            'Chrome/124.0.0.0 Safari/537.36'
        ),
        'Accept': 'application/pdf,text/html;q=0.9,*/*;q=0.8',
        'Accept-Language': 'en-US,en;q=0.9',
        'Connection': 'keep-alive',
    }

    parsed_host = pdf_url.split("/")[2] if "://" in pdf_url else ""
    if parsed_host:
        headers['Referer'] = f"https://{parsed_host}/"

    for attempt in range(retries + 1):

        try:

            response = requests.get(
                pdf_url,
                headers=headers,
                timeout=30,
                allow_redirects=True,
            )

            if response.status_code == 403:
                print(f"403 Forbidden (likely bot-blocked): {pdf_url}")
                return False

            if response.status_code != 200:
                print(f"Status code {response.status_code} for {pdf_url}")
                return False

            content_type = response.headers.get("content-type", "")

            if (
                "pdf" not in content_type.lower()
                and not response.url.endswith(".pdf")
            ):
                print(f"Warning: Not a PDF. Content-Type={content_type} for {pdf_url}")
                return False

            with open(save_path, "wb") as f:
                f.write(response.content)

            return True

        except requests.exceptions.RequestException as e:
            print(f"Download attempt {attempt + 1} failed for {pdf_url}: {e}")
            if attempt < retries:
                time.sleep(backoff)

    return False


def extract_pdf_text(path):

    doc = fitz.open(path)

    text = ""

    for page in doc:

        text += page.get_text()

    return text



def search_openalex(query, limit=7):

    try:

        response = requests.get(
            "https://api.openalex.org/works",
            params={
                "search": query,
                "per-page": limit,
                "filter":( f"from_publication_date:{min_year}-01-01,"
                "is_oa:true,"
                "has_fulltext:true")
            },
            timeout=15
        )

        response.raise_for_status()

        data = response.json()

        papers = []

        for paper in data["results"]:

            papers.append({
                "paper_id": paper.get("id"),

                "title": paper.get("title"),

                "year": paper.get("publication_year"),

                "abstract": "",

                "url": (
                    paper.get("primary_location", {})
                    .get("landing_page_url")
                ),

                "citation_count":
                    paper.get("cited_by_count", 0),

                "is_oa": (
                    paper.get("open_access", {})
                    .get("is_oa", False)
                ),

                "oa_url": (
                    paper.get("open_access", {})
                    .get("oa_url")
                ),

                "pdf_url": (
                    paper.get("primary_location", {})
                    .get("pdf_url")
                )
            })


        return papers

    except Exception as e:

        print("OpenAlex Error:", e)

        return []

def research_papers_node(state):
    search_queries = state["search_queries"]
    search_round = state.get(
        "search_round",
        0
    ) + 1
    allPapers =[]
    for query in search_queries:
        allPapers.extend( search_openalex(query , limit=5))

    print("Queries:", len(search_queries))
    print("Papers Found:", len(allPapers))
   
    return {
        "papers":allPapers,
        "search_round":search_round
    }
    


def de_deplicate_papers(state):
    papers = state["papers"]
    unique={}
    for paper in papers:
        title=paper["title"]
        unique[title]=paper
    
    return {
        "papers":list(unique.values())
    }

def route_query(state):

    if state["search_required"]:
        return "research_paper"

    return "chat_node"

def chat_node(state):

    response = model.invoke(
        state["user_query"]
    )

    return {
        "final_ans": response.content
    }



def pdf_downloader_node(state):
    query=state["user_query"]
    os.makedirs(
        f"papers_{query[:20]}",
        exist_ok=True
    )
   

   
    pdf_paths = []

    for idx, paper in enumerate(
        state["papers"]
    ):
        paper_id = paper["paper_id"].split("/")[-1]
        pdf_url = paper.get("pdf_url")

        if not pdf_url:
            continue

        path = f"papers_{query[:20]}/{paper_id}.pdf"
        downloaded_ids = state.get(
        "downloaded_ids",
         []
        )

        if paper["paper_id"] in downloaded_ids:
            continue
        success = download_pdf(
            pdf_url,
            path
        )

        if success:

            pdf_paths.append(path)
            downloaded_ids.append(
            paper["paper_id"]
)

            print(
                f"Downloaded: {paper['title']}"
            )

        else:

            print(
                f"Failed: {paper['title']}"
            )
    old_paths = state.get(
    "pdf_paths",
    []
)

    return {
    "pdf_paths": old_paths + pdf_paths,
    "downloaded_ids": downloaded_ids
}
   


def pdf_extractor_node(state):

    contents = []

    for path in state["pdf_paths"]:

        try:

            text = extract_pdf_text(
                path
            )

            contents.append(text)

        except Exception as e:

            print(e)

    return {
        "paper_contents": contents
    }





def route_after_download(state):

    pdf_count = len(
        state.get("pdf_paths", [])
    )

    search_round = state.get(
        "search_round",
        0
    )

    print(
        f"PDFs: {pdf_count}"
    )

    print(
        f"Round: {search_round}"
    )

    if pdf_count >= 10:
        return "pdf_extract"

    if search_round >=3:
        return "pdf_extract"

    return "Query_classifier"



from pydantic import  Field

class ReviewTask(BaseModel):
    task_name: str
    section: str = Field(
        description="Section of the literature review this task belongs to, "
                    "e.g. 'Model Performance Comparison', 'Dataset Comparison'"
    )
    objective: str
    relevant_papers: list[str] = Field(
        description="Titles of papers (from Available Papers) that are directly relevant to this task. "
                    "Only include papers that actually report data for this comparison."
    )
    comparison_metrics: list[str] = Field(
        description="Specific numeric metrics or fields to pull into the table, "
                    "e.g. ['R2', 'RMSE', 'dataset_size', 'model_used']"
    )
    instructions: str

class ReviewPlan(BaseModel):
    tasks: list[ReviewTask]





def planner_node(state):

    response = model.with_structured_output(
        ReviewPlan
    ).invoke(

        f"""
        You are an expert research review planner.

Your job is NOT to write the literature review.

Your job is to create a comprehensive set of research-analysis tasks that will later be executed by specialized worker agents.

Research Question:
{state["user_query"]}

Available Papers:
{state["paper_summaries"]}

Instructions:

1. Analyze the available papers and identify all major themes, methodologies, datasets, models, evaluation approaches, findings, limitations, and research directions.

2. Generate a detailed set of review tasks.

3. Each task must require COMPARISON across multiple papers, not summarization of individual papers.

4. Avoid generic tasks such as:

   * "Summarize Paper A"
   * "Describe methodology"
   * "Explain dataset"

5. Instead create analytical tasks such as:

   * Compare methodologies across studies
   * Compare model performance
   * Compare datasets and experimental settings
   * Compare evaluation metrics
   * Compare numerical results
   * Compare limitations
   * Compare contradictory findings
   * Compare sustainability approaches
   * Compare durability findings
   * Compare permeability findings
   * Compare mechanical properties
   * Compare environmental impacts
   * Identify trends over time
   * Identify research gaps
   * Identify future research opportunities

6. Generate as many tasks as required for a complete review.
   Do NOT restrict yourself to 5-8 tasks.
   Generate enough tasks to thoroughly analyze the literature.

7. Every task should:

   * cover a unique aspect
   * require cross-paper comparison
   * produce a detailed review section

8. Prioritize depth over brevity.

For each task provide:

* task_name
* section (the review section this task belongs to, e.g. "Model Performance Comparison")
* objective
* relevant_papers (EXACT titles, copied verbatim from Available Papers, of ONLY the papers that report data relevant to this task — do not include irrelevant papers, and do not invent titles)
* comparison_metrics (the exact numeric fields the worker must pull into a table, e.g. ["R2", "RMSE", "dataset_size"])
* instructions

The instructions should clearly tell a worker:

* to output a markdown TABLE (not prose) with one row per relevant paper and one column per comparison_metric
* to cite each paper as "Title (Year)" in the table
* to write "N/A" for any paper/metric not reported, never invent numbers
* to add a short 2-4 sentence takeaway after the table
* what trends, contradictions, or gaps to call out in that takeaway

The final collection of tasks should be sufficient to generate a 5-10+ page literature review when all worker outputs are combined.

        """
    )

    return {
        "review_plan": response.tasks
    }

class WorkerOutput(BaseModel):

    task_name: str

    findings: str

def worker_node(state):
    task = state["task"]

    relevant_titles = set(task.relevant_papers)
    filtered_summaries = [
        s for s in state["paper_summaries"]
        if s.get("title") in relevant_titles
    ]

    with gemini_semaphore:
        response = model.invoke(
            f"""
            You are a senior research analyst writing one detailed section of a literature review.
            Your output will be read on its own, so it must be elaborate, specific, and self-contained — not a brief summary.

            Task: {task.task_name}
            Objective: {task.objective}
            Metrics to extract: {task.comparison_metrics}

            Instructions:
            {task.instructions}

            Relevant Papers (use ONLY these papers, cite every one by "Title (Year)"):
            {filtered_summaries}

            HOW TO HANDLE MISSING NUMBERS — follow this decision rule for every metric cell:
            - If the paper's extracted data genuinely does not contain or measure this metric (it is not something that paper studied), write: "Not mentioned (NA)".
            - If the metric SHOULD logically be in the paper (e.g. the paper is clearly about model performance) but the extracted summary has it as null/empty — meaning the number likely exists in the original PDF but extraction failed to capture it — write: "Couldn't fetch".
            - Only write an actual number if it is explicitly present in the paper summary data. NEVER invent, estimate, or round a number that isn't there.
            - Use this judgment per cell, not per paper — the same paper can have some real numbers, some "Couldn't fetch", and some "Not mentioned (NA)" across different metrics.

            REQUIRED OUTPUT STRUCTURE:

            1. A markdown table. First column = Paper (Title, Year). One column per requested metric: {task.comparison_metrics}. Fill every cell using the decision rule above.

            2. A detailed discussion (aim for 300-500+ words, do not artificially shorten this) that:
               - Explicitly discusses EVERY paper listed in Relevant Papers by name — do not skip any, even if a paper contributes less than others; if a paper is less central to this task, say so explicitly and explain briefly why (e.g. "X (2022) reports no comparable benchmark here but is included for its methodological contrast").
               - Walks through what each paper specifically found, with exact numbers where available, always attached to the paper's title/year.
               - Explicitly calls out contradictions, agreements, or surprising gaps between papers.
               - Identifies any trend across publication years if relevant.
               - Does NOT pad with generic filler sentences ("this is an important area of research") — every sentence should carry a specific fact, comparison, or named paper.

            Do not omit any relevant paper from the discussion. Do not write a vague one-paragraph takeaway — this should be a thorough section, not a summary.
            """
        )

        content = response.content

        if isinstance(content, list):
            content = "\n".join(
                block.get("text", "")
                for block in content
                if isinstance(block, dict)
            )

    return {
        "worker_outputs": [
            {
                "task_name": task.task_name,
                "section": task.section,
                "content": content
            }
        ]
    }




from pydantic import BaseModel, Field
from typing import Optional


class PaperSummary(BaseModel):

    # 1. Bibliographic Information
    title: str
    publication_year: Optional[int] = None
    authors: Optional[list[str]] = None
    venue: Optional[str] = None
    country_region: Optional[str] = None

    # 2. Research Context
    research_problem: Optional[str] = None
    motivation: Optional[str] = None
    research_objective: Optional[str] = None
    research_questions: Optional[list[str]] = None

    # 3. Methodology
    methodology: Optional[str] = None
    experimental_setup: Optional[str] = None
    analytical_methods: Optional[list[str]] = None
    theoretical_framework: Optional[str] = None
    simulation_tools: Optional[list[str]] = None
    software_used: Optional[list[str]] = None

    # 4. Models and Techniques
    models_used: Optional[list[str]] = None
    algorithms_used: Optional[list[str]] = None
    statistical_methods: Optional[list[str]] = None
    optimization_methods: Optional[list[str]] = None
    machine_learning_methods: Optional[list[str]] = None

    # 5. Dataset Information
    dataset_name: Optional[str] = None
    dataset_source: Optional[str] = None
    dataset_size: Optional[str] = None
    input_features: Optional[list[str]] = None
    output_variables: Optional[list[str]] = None
    preprocessing_steps: Optional[list[str]] = None

    # 6. Evaluation
    evaluation_metrics: Optional[list[str]] = None
    validation_strategy: Optional[str] = None
    baseline_methods: Optional[list[str]] = None
    comparison_methods: Optional[list[str]] = None

    # 7. Numerical Results
    key_numerical_results: Optional[list[str]] = None
    reported_accuracy: Optional[str] = None
    reported_r2: Optional[str] = None
    reported_rmse: Optional[str] = None
    reported_mae: Optional[str] = None
    reported_permeability_values: Optional[str] = None
    reported_compressive_strength: Optional[str] = None
    other_quantitative_findings: Optional[list[str]] = None

    # 8. Main Findings
    key_findings: Optional[str] = None
    major_contributions: Optional[list[str]] = None
    practical_implications: Optional[str] = None

    # 9. Limitations
    stated_limitations: Optional[str] = None
    hidden_limitations_inferred: Optional[str] = None

    # 10. Future Work
    future_work: Optional[str] = None
    recommended_research_directions: Optional[list[str]] = None

    # 11. Comparative Insights
    advantages_of_proposed_method: Optional[list[str]] = None
    disadvantages_of_proposed_method: Optional[list[str]] = None
    comparison_with_prior_work: Optional[str] = None

    # 12. Evidence Extraction
    evidence_statements: list[str] = Field(
        default_factory=list,
        description="10-20 concise, factual statements directly supported by the paper, "
                    "e.g. 'ANN achieved R2 = 0.94 for compressive strength prediction.'"
    )

    # 13. Literature Review Utility
    themes_present: Optional[list[str]] = None
    research_gaps_identified: Optional[list[str]] = None
    contradictions_with_existing_literature: Optional[list[str]] = None
    trends_observed: Optional[list[str]] = None
    emerging_topics: Optional[list[str]] = None


def paper_summary_node(state):

    summaries = []

    for paper_text in state["paper_contents"]:
        time.sleep(10)
        response = model.with_structured_output(
            PaperSummary
        ).invoke(
            f"""
            You are a senior research analyst.

Your task is NOT to summarize the paper.

Your task is to extract every piece of information that may later be useful for a literature review, comparative analysis, meta-analysis, systematic review, or research gap identification.

Analyze the paper thoroughly and extract structured information.

Focus on factual extraction.

Do not invent information.

If information is unavailable, return null.

Extract:

1. Bibliographic Information

* title
* publication_year
* authors
* venue
* country/region (if identifiable)

2. Research Context

* research_problem
* motivation
* research_objective
* research_questions

3. Methodology

* methodology
* experimental_setup
* analytical_methods
* theoretical_framework
* simulation_tools
* software_used

4. Models and Techniques

* models_used
* algorithms_used
* statistical_methods
* optimization_methods
* machine_learning_methods

5. Dataset Information

* dataset_name
* dataset_source
* dataset_size
* input_features
* output_variables
* preprocessing_steps

6. Evaluation

* evaluation_metrics
* validation_strategy
* baseline_methods
* comparison_methods

7. Numerical Results

* key_numerical_results
* reported_accuracy
* reported_r2
* reported_rmse
* reported_mae
* reported_permeability_values
* reported_compressive_strength
* other_quantitative_findings

8. Main Findings

* key_findings
* major_contributions
* practical_implications

9. Limitations

* stated_limitations
* hidden_limitations_inferred_from_paper

10. Future Work

* future_work
* recommended_research_directions

11. Comparative Insights

* advantages_of_proposed_method
* disadvantages_of_proposed_method
* comparison_with_prior_work

12. Evidence Extraction

Extract 10-20 important evidence statements directly supported by the paper.

Each statement should be concise and factual.

Examples:

* ANN achieved R² = 0.94 for compressive strength prediction.
* Fly ash replacement improved permeability by 18%.
* Dataset contained 324 samples.
* Random Forest outperformed Gradient Boosting.

13. Literature Review Utility

Identify:

* themes_present
* research_gaps_identified
* contradictions_with_existing_literature
* trends_observed
* emerging_topics

Return highly detailed structured information.

Prioritize information useful for cross-paper comparison.
         Paper Text:
            {paper_text}
            """
        )

        summaries.append(
            response.model_dump()
        )

    return {
        "paper_summaries": summaries
    }

def dispatch_workers(state):

    sends = []

    for task in state["review_plan"]:

        sends.append(
            Send(
                "worker",
                {
                    "task": task,
                    "user_query": state["user_query"],
                    "paper_summaries": state["paper_summaries"],
                    "review_plan": state["review_plan"]
                }
            )
        )

    return sends
def synthesizer_node(state):

    paper_titles = [
        s.get("title", "Untitled")
        + (f" ({s.get('publication_year')})" if s.get("publication_year") else "")
        for s in state["paper_summaries"]
    ]

    worker_findings = "\n\n".join(
        f"### {o['task_name']}\n{o['content']}"
        for o in state["worker_outputs"]
    )

    response = model.invoke(
        f"""
        You are a senior research analyst writing the closing section of a literature review.

        Research Question:
        {state["user_query"]}

        Papers Reviewed (cite these by title/year, do not invent new ones):
        {paper_titles}

        Comparative Findings From All Tasks:
        {worker_findings}

        Write exactly two subsections:

        ## What Has Been Done
        Write a detailed bullet list of the concrete research contributions found across the reviewed papers.
        Every bullet must cite at least one specific paper as "Title (Year)" and should be 1-3 sentences — enough to state the method/model/dataset AND the specific result or number, not just name a topic.
        Be specific: name the method, model, dataset, or result. No vague statements like "researchers have explored X" without naming which paper and what they actually found.
        Cover every paper in {paper_titles} at least once across this list — do not leave any paper out.

        ## Future Research Opportunities
        Write a detailed bullet list of concrete, specific future research directions, grounded in gaps, limitations, or contradictions surfaced in the findings above.
        Each bullet should be 1-3 sentences: name the specific gap, explain WHY it's a gap (tie it to a limitation or missing comparison from a specific paper or set of papers, cited by title/year), and suggest what a follow-up study could concretely do differently.
        Avoid generic suggestions like "more research is needed" — be specific about what experiment, dataset, model, or comparison is missing.

        Both sections should be thorough bullet lists with real substance per bullet — not single-clause fragments, and not long unstructured paragraphs either. No repetition of the introduction.
        """
    )

    content = response.content
    if isinstance(content, list):
        content = "\n".join(
            block.get("text", "")
            for block in content
            if isinstance(block, dict)
        )

    return {
        "synthesis": content
    }

def assembler_node(state):

    review = f"# {state['user_query']}\n\n"

    papers_used = [
        f"- {s.get('title', 'Untitled')} ({s.get('publication_year', 'n.d.')})"
        for s in state["paper_summaries"]
    ]
    review += "## Papers Reviewed\n\n"
    review += "\n".join(papers_used) + "\n\n"

    sections: dict[str, list] = {}
    for output in state["worker_outputs"]:
        section = output.get("section", "General")
        sections.setdefault(section, []).append(output)

    for section, outputs in sections.items():

        review += f"## {section}\n\n"

        for output in outputs:
            review += f"### {output['task_name']}\n\n"
            review += f"{output['content']}\n\n"

    if state.get("synthesis"):
        review += f"{state['synthesis']}\n\n"

    return {
        "final_ans": review
    }


graph= StateGraph(state)
graph.add_node("Query_classifier",query_classifier_node)
graph.add_node("chat_node",chat_node)
graph.add_node("research_paper",research_papers_node)
graph.add_node("de_duplicate",de_deplicate_papers)
graph.add_node("pdf_download",pdf_downloader_node)
graph.add_node("pdf_extract",pdf_extractor_node)

graph.add_node("paper_summary",paper_summary_node)

graph.add_node("planner", planner_node)
graph.add_node("worker",worker_node)

graph.add_node("synthesizer", synthesizer_node)

graph.add_node(
    "assembler",
    assembler_node
)





graph.add_edge(START,"Query_classifier")
graph.add_conditional_edges(
    "Query_classifier",
    route_query,
    {
        "research_paper": "research_paper",
        "chat_node": "chat_node"
    }
)



graph.add_edge("research_paper","de_duplicate")
graph.add_edge("de_duplicate","pdf_download")
graph.add_conditional_edges("pdf_download",route_after_download,{
        "Query_classifier": "Query_classifier",
        "pdf_extract": "pdf_extract"
    })
graph.add_edge(
    "pdf_extract",
    "paper_summary"
)
graph.add_edge("paper_summary","planner")
graph.add_conditional_edges(
    "planner",
    dispatch_workers
)
graph.add_edge(
    "worker",
    "synthesizer"
)
graph.add_edge(
    "synthesizer",
    "assembler"
)

graph.add_edge(
    "assembler",
    END
)
# graph.add_edge("planner","paper_summary")

graph.add_edge("chat_node",END)

chatbot = graph.compile()


result = chatbot.invoke(
    {
        "user_query":
        "using machine learning to predict strenght of permeable pervious concrete"
    }
)

final_review = result["final_ans"]

with open(
    "literature_review.md",
    "w",
    encoding="utf-8"
) as f:
    f.write(final_review)

print("Saved to literature_review.md")

png_data = chatbot.get_graph().draw_mermaid_png()

with open("graph.png", "wb") as f:
    f.write(png_data)




