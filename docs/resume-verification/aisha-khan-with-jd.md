# aisha-khan.pdf — version 7 — with a job description
status: **success**

<details><summary>Job description used</summary>

Junior Data Analyst - BrightRetail

Join our analytics team to turn retail data into decisions.
- Write SQL queries and build dashboards in Tableau or Power BI
- Clean and analyse data with Python (pandas) or Excel
- Present insights to non-technical stakeholders
- Run A/B test analysis and report results
Requirements: degree in a quantitative field, SQL, basic statistics, strong communication, internship or project experience.


</details>

## ATS Compatibility Agent — success
_gemini / gemini-3.5-flash-lite, 1540 ms, 1 reply(ies)_
```json
{
  "score": 90,
  "issues": [
    {
      "fix": "Add a brief 'Summary' section right below your contact details highlighting your background in mathematics, statistics, and data analysis.",
      "title": "Missing Summary Section",
      "source": "agent",
      "category": "missing_section",
      "evidence": "The parsed structure and text show no Summary section, jumping straight from contact details to Education and Work Experience.",
      "severity": "warning",
      "explanation": "The resume does not include a professional summary section at the top, which helps applicant tracking systems and hiring managers quickly grasp your core qualifications."
    }
  ],
  "summary": "The resume has a clean single-column layout that parses reliably, but it is missing a dedicated Summary section. Adding an introduction will help recruiters quickly understand your profile."
}
```

## Parser Agent — success
_gemini / gemini-3.5-flash-lite, 2158 ms, 1 reply(ies)_
```json
{
  "skills": [
    "Regression",
    "Probability",
    "Databases",
    "R programming",
    "Excel",
    "SQL",
    "R"
  ],
  "summary": "",
  "education": [
    {
      "dates": "2020 - 2023",
      "degree": "BSc Mathematics and Statistics",
      "details": "2:1. Modules: Regression, Probability, Databases, R programming.",
      "institution": "University of Manchester"
    }
  ],
  "experience": [
    {
      "dates": "Jun 2022 - Sep 2022",
      "title": "Data Intern",
      "bullets": [
        "Helped the analytics team with weekly reports.",
        "Used Excel and some SQL to look at sales data.",
        "Made charts for the manager."
      ],
      "company": "GreenGrocer Ltd"
    },
    {
      "dates": "2019 - 2022",
      "title": "Customer Assistant",
      "bullets": [
        "Served customers and handled cash.",
        "Trained 3 new team members during busy periods."
      ],
      "company": "Costa Coffee"
    },
    {
      "dates": "",
      "title": "",
      "bullets": [
        "analysed 12,000 rows of NHS appointment data in R to study missed-appointment rates."
      ],
      "company": "Dissertation"
    }
  ],
  "contact_info": {
    "name": "Aisha Khan",
    "email": "aisha.khan@example.com",
    "links": [],
    "phone": "+44 7700 900123",
    "location": "Manchester, UK"
  }
}
```

## Content & Impact Agent — success
_gemini / gemini-3.5-flash-lite, 2098 ms, 1 reply(ies)_
```json
{
  "bullets": [
    {
      "id": "e0.b0",
      "flags": [
        "weak_opening_verb",
        "no_metric"
      ],
      "rewrite": "Supported the analytics team with [N] weekly reports.",
      "explanation": "Starts with the weak opening verb 'Helped' and lacks a metric for the volume of weekly reports."
    },
    {
      "id": "e0.b1",
      "flags": [
        "weak_opening_verb",
        "no_metric"
      ],
      "rewrite": "Analyzed sales data using Excel and SQL across [N] datasets.",
      "explanation": "Uses the weak opening verb 'Used' and vague phrasing like 'some SQL' with no metrics on dataset size or frequency."
    },
    {
      "id": "e0.b2",
      "flags": [
        "weak_opening_verb",
        "no_metric"
      ],
      "rewrite": "Generated [N] charts for the manager.",
      "explanation": "Starts with the weak opening verb 'Made' and lacks a metric for the number of charts created."
    },
    {
      "id": "e1.b0",
      "flags": [
        "weak_opening_verb",
        "no_metric"
      ],
      "rewrite": "Served customers and handled cash transactions totaling [amount] or [N] transactions daily.",
      "explanation": "Starts with the weak opening verb 'Served' and provides no metrics regarding transaction volume or customer count."
    },
    {
      "id": "e1.b1",
      "flags": [],
      "rewrite": "",
      "explanation": ""
    },
    {
      "id": "e2.b0",
      "flags": [],
      "rewrite": "",
      "explanation": ""
    }
  ],
  "overall": "The resume lacks quantitative impact and relies heavily on weak opening verbs and vague descriptions. Many bullets fail to specify the scope of work or tangible outcomes achieved.",
  "summary_review": ""
}
```

## Text and layout extraction — success
```json
{
  "layout": {
    "fonts": [
      "Helvetica",
      "Helvetica-Bold"
    ],
    "pages": [
      {
        "page": 1,
        "links": 0,
        "images": 0,
        "tables": [],
        "columns": 1,
        "text_chars": 654,
        "icon_images": 0,
        "left_blocks": 0,
        "text_blocks": 14,
        "large_images": 0,
        "right_blocks": 0,
        "footer_zone_text": "",
        "header_zone_text": "",
        "side_by_side_pairs": 0
      }
    ],
    "font_count": 2,
    "page_count": 1,
    "section_headers": [
      "Aisha Khan",
      "EDUCATION",
      "WORK EXPERIENCE",
      "PROJECTS"
    ],
    "total_text_chars": 654,
    "nonstandard_headers": [
      "Aisha Khan"
    ],
    "contact_in_header_footer": false
  },
  "needs_ocr": false,
  "char_count": 653,
  "page_count": 1
}
```

## Rewrite Agent — success
_gemini / gemini-3.5-flash-lite, 2546 ms, 1 reply(ies)_
```json
{
  "notes": "Added a concise professional summary highlighting the candidate's quantitative education and data analysis background to address the missing summary ATS warning. Upgraded weak opening verbs and added bracketed metric placeholders to internship and customer service bullets while preserving all original facts and constraints.",
  "skills": [
    "SQL",
    "Excel",
    "Regression",
    "Probability",
    "Databases",
    "R programming"
  ],
  "summary": "Junior Data Analyst with a BSc in Mathematics and Statistics and hands-on internship experience in SQL, Excel, and data reporting. Proven ability to analyze datasets, generate charts, and apply statistical methods to support business insights.",
  "experience": [
    {
      "dates": "Jun 2022 - Sep 2022",
      "title": "Data Intern",
      "bullets": [
        {
          "text": "Analyzed sales data using Excel and SQL across [N] datasets.",
          "source_id": "e0.b1"
        },
        {
          "text": "Supported the analytics team with [N] weekly reports.",
          "source_id": "e0.b0"
        },
        {
          "text": "Generated [N] charts for the manager.",
          "source_id": "e0.b2"
        }
      ],
      "company": "GreenGrocer Ltd"
    },
    {
      "dates": "2019 - 2022",
      "title": "Customer Assistant",
      "bullets": [
        {
          "text": "Served customers and handled cash transactions totaling [amount] or [N] transactions daily.",
          "source_id": "e1.b0"
        },
        {
          "text": "Trained 3 new team members during busy periods.",
          "source_id": "e1.b1"
        }
      ],
      "company": "Costa Coffee"
    },
    {
      "dates": "",
      "title": "",
      "bullets": [
        {
          "text": "analysed 12,000 rows of NHS appointment data in R to study missed-appointment rates.",
          "source_id": "e2.b0"
        }
      ],
      "company": "Dissertation"
    }
  ]
}
```

## Job-Match Agent — success
_gemini / gemini-3.5-flash-lite, 3693 ms, 1 reply(ies)_
```json
{
  "summary": "The candidate has a solid quantitative educational background and relevant internship experience in data analysis using SQL and Excel. The biggest gap is the lack of Python, Tableau/Power BI, and A/B testing experience.",
  "match_score": 72,
  "matched_keywords": [
    {
      "keyword": "SQL",
      "evidence": "Job 0 (e0.b1) and Skills section"
    },
    {
      "keyword": "Excel",
      "evidence": "Job 0 (e0.b1) and Skills section"
    },
    {
      "keyword": "quantitative field",
      "evidence": "BSc Mathematics and Statistics"
    },
    {
      "keyword": "communication",
      "evidence": "Job 0 (e0.b2) and Job 1 (e1.b0, e1.b1)"
    },
    {
      "keyword": "internship or project experience",
      "evidence": "Data Intern at GreenGrocer Ltd and Dissertation project"
    },
    {
      "keyword": "statistics",
      "evidence": "BSc Mathematics and Statistics degree and coursework modules"
    }
  ],
  "missing_keywords": [
    {
      "keyword": "Python",
      "importance": "high",
      "suggestion": "True gap; candidate used R instead for dissertation."
    },
    {
      "keyword": "Tableau or Power BI",
      "importance": "high",
      "suggestion": "True gap; charts were made for the manager in Job 0 using Excel/basic tools."
    },
    {
      "keyword": "A/B test analysis",
      "importance": "medium",
      "suggestion": "True gap; statistical background exists, but no direct A/B testing mentioned."
    }
  ],
  "requirement_matches": [
    {
      "note": "Shows SQL and chart creation, but lacks specific mention of Tableau or Power BI.",
      "strength": "partial",
      "bullet_ids": [
        "e0.b1",
        "e0.b2"
      ],
      "requirement": "Write SQL queries and build dashboards in Tableau or Power BI"
    },
    {
      "note": "Shows Excel usage in the internship and R for the dissertation, though Python is missing.",
      "strength": "strong",
      "bullet_ids": [
        "e0.b1",
        "e2.b0"
      ],
      "requirement": "Clean and analyse data with Python (pandas) or Excel"
    },
    {
      "note": "Made charts for the manager, but broader stakeholder presentation is not explicitly detailed.",
      "strength": "partial",
      "bullet_ids": [
        "e0.b2"
      ],
      "requirement": "Present insights to non-technical stakeholders"
    },
    {
      "note": "No A/B testing experience shown in the resume.",
      "strength": "none",
      "bullet_ids": [],
      "requirement": "Run A/B test analysis and report results"
    },
    {
      "note": "Shown by BSc Mathematics and Statistics in the Education section.",
      "strength": "strong",
      "bullet_ids": [],
      "requirement": "Degree in a quantitative field"
    },
    {
      "note": "Covered by degree title, coursework modules (Probability, Regression), and dissertation data analysis.",
      "strength": "strong",
      "bullet_ids": [],
      "requirement": "Basic statistics"
    },
    {
      "note": "Demonstrated through customer service and team training experience, though not explicitly tailored to data storytelling.",
      "strength": "partial",
      "bullet_ids": [
        "e1.b0",
        "e1.b1"
      ],
      "requirement": "Strong communication"
    },
    {
      "note": "Clearly demonstrated via the Data Intern role at GreenGrocer Ltd and the university dissertation project.",
      "strength": "strong",
      "bullet_ids": [
        "e0.b0",
        "e0.b1",
        "e2.b0"
      ],
      "requirement": "Internship or project experience"
    }
  ]
}
```

## Final result

**Stats:** {"jd": true, "ats_score": 90, "corrections": 0, "flag_counts": {"no_metric": 4, "vague_claim": 0, "passive_voice": 0, "weak_opening_verb": 4}, "match_score": 72, "ats_blockers": 0, "ats_warnings": 1, "missing_high": ["Python", "Tableau or Power BI"], "placeholders": 5, "ats_remaining": ["Missing Summary Section"], "bullets_total": 6, "keywords_total": 9, "bullets_flagged": 4, "summary_changed": true, "keywords_matched": 6, "bullets_reordered": 2, "bullets_rewritten": 4, "ats_fixed_by_reformat": 0}

**Rewrite notes:** Added a concise professional summary highlighting the candidate's quantitative education and data analysis background to address the missing summary ATS warning. Upgraded weak opening verbs and added bracketed metric placeholders to internship and customer service bullets while preserving all original facts and constraints.

### Before / after, per bullet

- **e0.b1** (GreenGrocer Ltd) CHANGED ['weak_opening_verb', 'no_metric']
  - before: Used Excel and some SQL to look at sales data.
  - after:  Analyzed sales data using Excel and SQL across [N] datasets.
- **e0.b0** (GreenGrocer Ltd) CHANGED ['weak_opening_verb', 'no_metric']
  - before: Helped the analytics team with weekly reports.
  - after:  Supported the analytics team with [N] weekly reports.
- **e0.b2** (GreenGrocer Ltd) CHANGED ['weak_opening_verb', 'no_metric']
  - before: Made charts for the manager.
  - after:  Generated [N] charts for the manager.
- **e1.b0** (Costa Coffee) CHANGED ['weak_opening_verb', 'no_metric']
  - before: Served customers and handled cash.
  - after:  Served customers and handled cash transactions totaling [amount] or [N] transactions daily.
- **e1.b1** (Costa Coffee) kept []
  - before: Trained 3 new team members during busy periods.
- **e2.b0** (Dissertation) kept []
  - before: analysed 12,000 rows of NHS appointment data in R to study missed-appointment rates.

### Rewritten draft

**Aisha Khan** · aisha.khan@example.com · +44 7700 900123 · Manchester, UK

**Summary.** Junior Data Analyst with a BSc in Mathematics and Statistics and hands-on internship experience in SQL, Excel, and data reporting. Proven ability to analyze datasets, generate charts, and apply statistical methods to support business insights.

**Data Intern, GreenGrocer Ltd** (Jun 2022 - Sep 2022)
- Analyzed sales data using Excel and SQL across [N] datasets.
- Supported the analytics team with [N] weekly reports.
- Generated [N] charts for the manager.

**Customer Assistant, Costa Coffee** (2019 - 2022)
- Served customers and handled cash transactions totaling [amount] or [N] transactions daily.
- Trained 3 new team members during busy periods.

**, Dissertation** ()
- analysed 12,000 rows of NHS appointment data in R to study missed-appointment rates.

**Education.** BSc Mathematics and Statistics, University of Manchester (2020 - 2023)

**Skills.** SQL, Excel, Regression, Probability, Databases, R programming
