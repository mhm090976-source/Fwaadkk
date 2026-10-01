<!DOCTYPE html>
<html lang="ar" dir="rtl">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Forex Radar - رادار أخبار الفوركس</title>
    <style>
        :root {
            --bg-color: #0f172a;
            --card-bg: #1e293b;
            --text-color: #f8fafc;
            --accent-color: #38bdf8;
            --high-impact: #ef4444;
            --medium-impact: #f59e0b;
            --low-impact: #10b981;
        }

        body {
            font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
            background-color: var(--bg-color);
            color: var(--text-color);
            margin: 0;
            padding: 20px;
        }

        .header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            max-width: 800px;
            margin: 0 auto 20px auto;
            border-bottom: 2px solid #334155;
            padding-bottom: 15px;
        }

        h1 {
            margin: 0;
            font-size: 1.5rem;
            color: var(--accent-color);
        }

        .subscribe-btn {
            background-color: #2563eb;
            color: white;
            border: none;
            padding: 10px 18px;
            border-radius: 8px;
            font-weight: bold;
            cursor: pointer;
            transition: 0.3s;
        }

        .subscribe-btn:hover {
            background-color: #1d4ed8;
        }

        .news-container {
            max-width: 800px;
            margin: 0 auto;
        }

        .news-card {
            background-color: var(--card-bg);
            border-radius: 10px;
            padding: 15px;
            margin-bottom: 15px;
            box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.1);
        }

        .news-title {
            font-size: 1.1rem;
            margin-bottom: 10px;
            text-decoration: none;
            color: var(--text-color);
            display: block;
        }

        .news-title:hover {
            color: var(--accent-color);
        }

        .meta-info {
            display: flex;
            gap: 10px;
            font-size: 0.85rem;
            color: #94a3b8;
            align-items: center;
        }

        .badge {
            padding: 3px 8px;
            border-radius: 4px;
            font-size: 0.75rem;
            font-weight: bold;
            color: white;
        }

        .badge.high { background-color: var(--high-impact); }
        .badge.medium { background-color: var(--medium-impact); }
        .badge.low { background-color: var(--low-impact); }
    </style>
</head>
<body>

    <div class="header">
        <h1>📡 Forex Radar</h1>
        <button class="subscribe-btn" onclick="subscribeUsdt()">⭐ اشتراك VIP ($10 USDT)</button>
    </div>

    <div class="news-container" id="news-container">
        <p style="text-align: center;">جاري تحميل الأخبار الحية...</p>
    </div>

    <script>
        // 1. جلب الأخبار من Backend
        async function fetchNews() {
            try {
                const response = await fetch('/api/news');
                const result = await response.json();
                
                const container = document.getElementById('news-container');
                container.innerHTML = '';

                if (result.status === 'success' && result.data.length > 0) {
                    result.data.forEach(item => {
                        const card = document.createElement('div');
                        card.className = 'news-card';
                        
                        const impactText = item.impact === 'high' ? 'عالي التأثير' : (item.impact === 'medium' ? 'متوسط' : 'عادي');
                        
                        card.innerHTML = `
                            <a href="${item.link}" target="_blank" class="news-title">${item.title}</a>
                            <div class="meta-info">
                                <span class="badge ${item.impact}">${impactText}</span>
                                <span>المصدر: ${item.source}</span>
                                <span>• ${item.published}</span>
                            </div>
                        `;
                        container.appendChild(card);
                    });
                } else {
                    container.innerHTML = '<p style="text-align: center;">لا توجد أخبار متاحة حالياً.</p>';
                }
            } catch (err) {
                console.error(err);
                document.getElementById('news-container').innerHTML = '<p style="text-align: center; color: red;">حدث خطأ أثناء الاتصال بالخادم.</p>';
            }
        }

        // 2. دالة بدء طلب الاشتراك وإنشاء فاتورة USDT
        async function subscribeUsdt() {
            const btn = document.querySelector('.subscribe-btn');
            btn.innerText = 'جاري تحضير الفاتورة...';
            btn.disabled = true;

            try {
                const response = await fetch('/api/create-invoice', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' }
                });
                const data = await response.json();

                if (data.status === 'success') {
                    // توجيه المشترك مباشرة لصفحة الدفع في Cryptomus
                    window.location.href = data.payment_url;
                } else {
                    alert('خطأ: ' + (data.message || 'يرجى التأكد من إضافة API Key أولاً.'));
                    btn.innerText = '⭐ اشتراك VIP ($10 USDT)';
                    btn.disabled = false;
                }
            } catch (err) {
                alert('فشل الاتصال بالخادم.');
                btn.innerText = '⭐ اشتراك VIP ($10 USDT)';
                btn.disabled = false;
            }
        }

        // تحميل الأخبار فور فتح الصفحة
        fetchNews();
    </script>
</body>
</html>
