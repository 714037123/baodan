import streamlit as st
import re
import pdfplumber
import tempfile
import pandas as pd
from PIL import Image, ImageEnhance, ImageFilter
import pytesseract
from pdf2image import convert_from_path


# ===================== 通用文本清洗 =====================
def clean_text(raw_text):
    if not raw_text:
        return ""
    text = raw_text.replace("\r", "\n").replace("\t", " ")
    lines = []
    for line in text.splitlines():
        line = line.strip()
        if line:
            line = re.sub(r"\s+", " ", line)
            lines.append(line)
    return "\n".join(lines)


# ===================== OCR图像预处理 大幅优化识别准确率 =====================
def ocr_preprocess(img):
    w, h = img.size
    img = img.resize((w * 2, h * 2), Image.Resampling.LANCZOS)
    img = ImageEnhance.Contrast(img).enhance(2.2)
    img = ImageEnhance.Brightness(img).enhance(1.3)
    img = ImageEnhance.Sharpness(img).enhance(1.8)
    img = img.convert("L")
    img = img.filter(ImageFilter.MedianFilter(size=1))
    threshold = 130
    table = [0 if i < threshold else 255 for i in range(256)]
    img = img.point(table, '1')
    return img


def get_ocr_full_text(pdf_path):
    pages = convert_from_path(pdf_path, dpi=300)
    full_raw = ""
    for idx, page in enumerate(pages, 1):
        img = ocr_preprocess(page)
        config = r'--oem 3 --psm 6 -c preserve_interword_spaces=1'
        raw_txt = pytesseract.image_to_string(img, lang="chi_sim+eng", config=config)
        full_raw += f"\n====OCR第{idx}页====\n{raw_txt}"
    return clean_text(full_raw)


# ===================== 大写人民币转数字工具函数 =====================
def chinese_money_to_num(cn_str):
    digit_map = {'零': 0, '壹': 1, '贰': 2, '叁': 3, '肆': 4, '伍': 5, '陆': 6, '柒': 7, '捌': 8, '玖': 9}
    unit_map = {'分': 0.01, '角': 0.1, '元': 1, '拾': 10, '佰': 100, '仟': 1000, '万': 10000}
    num = 0.0
    temp = 0
    unit = 1
    point_flag = False
    for char in cn_str:
        if char in digit_map:
            temp = digit_map[char]
        elif char == '元':
            num += temp * unit
            temp = 0
            unit = unit_map['元']
            point_flag = True
        elif char == '角':
            num += temp * unit_map['角']
            temp = 0
        elif char == '分':
            num += temp * unit_map['分']
            temp = 0
        elif char in unit_map and point_flag is False:
            unit = unit_map[char]
    num += temp * unit
    return round(num, 2)


# ===================== 核心字段提取函数（精准分块抓取+兜底，无串值） =====================
def extract_field_data(text):
    res = {
        "保单生成时间": "未识别",
        "被保险人姓名": "未识别",
        "身份证号码": "未识别",
        "车架号": "未识别",
        "厂牌型号": "未识别",
        "交强险保单号": "未识别",
        "商业险保单号": "未识别",
        "交强险保费": "未识别",
        "商业险保费": "未识别",
        "三者险保额(万)": "未识别",
        "商业险生效时间": "未识别",
        "驾乘险保单号": "未识别",
        "驾乘险保费": "未识别"
    }

    # 1. 保单生成时间
    m = re.search(r"生成保单时间[:：]\s*(\d{4}-\d{2}-\d{2} \d{2}:\d{2})", text)
    if m:
        res["保单生成时间"] = m.group(1)

    # 2. 被保险人姓名
    m = re.search(r"被\s*保\s*险\s*人\s+([\u4e00-\u9fa5]{2,4})", text)
    if not m:
        m = re.search(r"被保险人\s*([\u4e00-\u9fa5]{2,4})", text)
    if m:
        res["被保险人姓名"] = m.group(1)

    # 3. 身份证号码
    m = re.search(r"被保险人身份证号码[^0-9Xx]*(\d{17}[\dXx])", text)
    if m:
        res["身份证号码"] = m.group(1)

    # 4. 车架号
    m = re.search(r"识别代码\(车架号\)\s*([A-Z0-9]{17})", text)
    if not m:
        m = re.search(r"VIN码/车架号\s*([A-Z0-9]{17})", text)
    if m:
        res["车架号"] = m.group(1)

    # 5. 厂牌型号 兼容燃油/新能源轿车，取消纯电动限制
    m = re.search(r"厂牌型号\s*([\u4e00-\u9fa5A-Z0-9]+轿车)", text)
    if m:
        res["厂牌型号"] = m.group(1)

    # 6. 三类保单号 PDZA交强 / PDAA商业 / PEBS驾乘
    m_jq = re.search(r"保险单号[:：]\s*(PDZA[0-9]+)", text)
    m_sy = re.search(r"保险单号[:：]\s*(PDAA[0-9]+)", text)
    m_jc = re.search(r"保险单号[:：]\s*(PEBS[0-9]+)", text)
    if m_jq:
        res["交强险保单号"] = m_jq.group(1)
    if m_sy:
        res["商业险保单号"] = m_sy.group(1)
    if m_jc:
        res["驾乘险保单号"] = m_jc.group(1)

    # 统一保费通用正则：兼容两种括号、任意空格换行、正确小数点转义
    fee_rule = r"保险费\s*合计\s*[（:：]?\s*人民币?[大写）：]*[\u4e00-玖零元角分]+.*?[¥￥]\s*[:：]?\s*([0-9,]+?\.\d{2})"

    # ========== 第一步：分区块精准抓取，优先赋值，不会串金额 ==========
    # 1. 交强险区块抓取
    jq_area = re.search(r"机动车交通事故责任强制保险单[\s\S]*?(?====PDF原生|机动车商业保险保险单)", text)
    if jq_area:
        jq_match = re.search(fee_rule, jq_area.group())
        if jq_match:
            res["交强险保费"] = jq_match.group(1).replace(",", "")

    # 2. 商业险区块抓取
    sy_area = re.search(r"机动车商业保险保险单[\s\S]*?(?====PDF原生|“如意行”驾乘综合保险保险单)", text)
    if sy_area:
        sy_match = re.search(fee_rule, sy_area.group())
        if sy_match:
            res["商业险保费"] = sy_match.group(1).replace(",", "")

    # 3. 驾乘险区块抓取
    jc_area = re.search(r"“如意行”驾乘综合保险保险单[\s\S]*?(?====PDF原生|保险条款清单)", text)
    if jc_area:
        jc_match = re.search(fee_rule, jc_area.group())
        if jc_match:
            res["驾乘险保费"] = jc_match.group(1).replace(",", "")

    # ========== 第二步：全局兜底补充，只填充未识别字段，不覆盖已抓到的值 ==========
    all_fee_list = re.findall(fee_rule, text)
    target_fee_keys = ["交强险保费", "商业险保费", "驾乘险保费"]
    for index, money_str in enumerate(all_fee_list):
        if index >= len(target_fee_keys):
            break
        key = target_fee_keys[index]
        # 只有当前还是未识别，才用全局数值填充，精准抓取到的数值保留不动
        if res[key] == "未识别":
            res[key] = money_str.replace(",", "")

    # 三者险保额：同时匹配机动车/新能源第三者
    m_three = re.search(r"(新能源汽车|机动车)第三者责任保险\s*/?\s*([0-9,]+)\.\d{2}", text)
    if m_three:
        num_str = m_three.group(2).replace(",", "")
        res["三者险保额(万)"] = str(int(num_str) // 10000)

    # 商业险生效时间，移除新能源前缀通用匹配
    m_time = re.search(r"保险期间 自(\d{4}年\d{2}月\d{2}日\d{1,2})", text)
    if m_time:
        res["商业险生效时间"] = m_time.group(1)

    return res


# ===================== PDF与OCR结果对比择优函数 =====================
def compare_select_correct(pdf_data, ocr_data):
    final = {}
    for key in pdf_data.keys():
        val_pdf = pdf_data[key]
        val_ocr = ocr_data[key]
        if val_pdf != "未识别" and val_ocr != "未识别" and val_pdf == val_ocr:
            final[key] = val_pdf
        elif val_pdf != "未识别" and val_ocr != "未识别" and val_pdf != val_ocr:
            final[key] = f"【数据冲突】PDF:{val_pdf} | OCR:{val_ocr}"
        elif val_pdf != "未识别":
            final[key] = val_pdf
        elif val_ocr != "未识别":
            final[key] = val_ocr
        else:
            final[key] = "未识别"
    return final


# ===================== 单PDF解析入口（仅扫描件运行OCR） =====================
def parse_pdf_single(upload_file):
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
        tmp.write(upload_file.read())
        tmp_path = tmp.name

    # 通路1：PDF原生文字提取
    pdf_raw_text = ""
    try:
        with pdfplumber.open(tmp_path) as pdf:
            for idx, page in enumerate(pdf.pages, 1):
                page_txt = page.extract_text()
                if page_txt:
                    pdf_raw_text += f"\n====PDF原生第{idx}页====\n{page_txt}"
    except Exception as e:
        pdf_raw_text = ""
    pdf_clean = clean_text(pdf_raw_text)
    pdf_result = extract_field_data(pdf_clean)

    # 通路2：OCR按需执行：原生文字为空=扫描件才运行OCR
    ocr_clean = ""
    ocr_result = {k: "未识别" for k in pdf_result.keys()}
    if pdf_clean.strip() == "":
        try:
            ocr_clean = get_ocr_full_text(tmp_path)
            ocr_result = extract_field_data(ocr_clean)
        except Exception as e:
            ocr_clean = "OCR识别失败"

    final_result = compare_select_correct(pdf_result, ocr_result)
    return final_result, pdf_result, ocr_result, pdf_clean, ocr_clean


# ===================== Streamlit前端页面 =====================
st.set_page_config(page_title="人保三合一保单批量识别工具", layout="wide")
st.title("人保三合一保单批量识别工具")
st.markdown("识别逻辑：PDF原生文字优先，仅扫描图片PDF自动启用OCR备份，**支持直接拖拽批量PDF上传**")

# 【原生自带拖拽区域】修改提示文案，明确告知可拖拽
uploaded_files = st.file_uploader(
    label="📂 批量上传保单PDF（可直接拖拽多个文件到下方区域，也可点击选择）",
    type="pdf",
    accept_multiple_files=True,
    help="支持电子保单、扫描件PDF，多文件一次性拖拽上传"
)

# 存储所有文件的汇总结果
all_final_results = []
all_file_details = {}

if uploaded_files:
    st.success(f"已加载 {len(uploaded_files)} 个PDF文件，开始解析...")
    with st.spinner(f"正在批量解析 {len(uploaded_files)} 个保单文件，请稍候..."):
        for file in uploaded_files:
            try:
                # 单个文件解析
                final_data, pdf_data, ocr_data, pdf_text, ocr_text = parse_pdf_single(file)
                # 加入汇总列表
                final_data["文件名"] = file.name
                final_data["处理状态"] = "✅ 解析成功"
                all_final_results.append(final_data)
                # 保存每个文件的详细数据，用于展开预览
                all_file_details[file.name] = {
                    "final": final_data,
                    "pdf": pdf_data,
                    "ocr": ocr_data,
                    "pdf_text": pdf_text,
                    "ocr_text": ocr_text
                }
            except Exception as e:
                # 异常处理，单个文件失败不影响其他文件
                error_data = {
                    "文件名": file.name,
                    "处理状态": f"❌ 解析失败：{str(e)}",
                    "保单生成时间": "未识别",
                    "被保险人姓名": "未识别",
                    "身份证号码": "未识别",
                    "车架号": "未识别",
                    "厂牌型号": "未识别",
                    "交强险保单号": "未识别",
                    "商业险保单号": "未识别",
                    "交强险保费": "未识别",
                    "商业险保费": "未识别",
                    "三者险保额(万)": "未识别",
                    "商业险生效时间": "未识别",
                    "驾乘险保单号": "未识别",
                    "驾乘险保费": "未识别"
                }
                all_final_results.append(error_data)
                st.error(f"文件【{file.name}】解析失败：{str(e)}")

    # 转换为DataFrame，生成汇总Excel
    if all_final_results:
        df_summary = pd.DataFrame(all_final_results)
        # 调整列顺序，把文件名、状态放最前面
        cols = df_summary.columns.tolist()
        cols.insert(0, cols.pop(cols.index("文件名")))
        cols.insert(1, cols.pop(cols.index("处理状态")))
        df_summary = df_summary[cols]

        # 显示汇总表格
        st.subheader("📊 批量解析汇总结果")
        st.dataframe(df_summary, use_container_width=True)

        # 生成Excel文件，提供下载
        with tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False) as excel_tmp:
            df_summary.to_excel(excel_tmp, index=False, sheet_name="保单汇总")
            excel_tmp_path = excel_tmp.name

        with open(excel_tmp_path, "rb") as f:
            st.download_button(
                label="📥 下载全部结果汇总Excel",
                data=f,
                file_name="人保保单批量识别汇总.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            )

        # 每个文件的详细结果展开预览
        st.subheader("📋 单个文件详细识别结果（可展开查看）")
        for file_name, details in all_file_details.items():
            with st.expander(f"📄 {file_name} - 详细结果"):
                # 最终择优结果
                st.subheader("✅ 对比筛选后最终正确数据")
                st.table(details["final"])

                # 分栏原始结果
                col1, col2 = st.columns(2)
                with col1:
                    st.subheader("① PDF原生文字提取结果（优先采信）")
                    st.table(details["pdf"])
                with col2:
                    st.subheader("② OCR图像识别备份结果（仅扫描件有内容）")
                    st.table(details["ocr"])

                # 完整原文
                with st.expander("查看PDF原生完整文本"):
                    st.code(details["pdf_text"], language="text")
                with st.expander("查看OCR识别完整文本（电子PDF为空）"):
                    st.code(details["ocr_text"], language="text")