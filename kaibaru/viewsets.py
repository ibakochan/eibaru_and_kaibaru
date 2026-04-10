from django.utils import timezone
from rest_framework import viewsets, serializers, status
from rest_framework.decorators import action
from rest_framework.response import Response
from .models import Member, Club, Lesson, Participation, SlateImage, JoinRequest, MembershipPlan, Subscription
from accounts.models import CustomUser
from django.contrib.auth import login
import re
import unicodedata
import json
import logging

import stripe
from django.conf import settings
from django.db.models import Prefetch

stripe.api_key = settings.STRIPE_SECRET_KEY

import uuid


from .serializers import MembershipPlanSerializer, MemberSerializer, ClubSerializer, LessonSerializer, ParticipationSerializer, SlateImageSerializer, JoinRequestSerializer
from django.conf import settings
from datetime import timedelta
import hashlib

from django.db import transaction

from .utils import sync_member_quantity

from django.core.files.base import ContentFile
import os


from collections import defaultdict
def get_level_participation(member):
    level_sums = defaultdict(int)
    for p in member.participations.all():
        if p.level_counts:
            for lvl, count in p.level_counts.items():
                level_sums[int(lvl)] += count
    return dict(level_sums)

VALID_SUBDOMAIN_RE = re.compile(r'^[a-z0-9-]+$', re.IGNORECASE)



def hash_uploaded_file(uploaded_file, chunk_size=8192):
    hasher = hashlib.sha256()
    for chunk in uploaded_file.chunks(chunk_size):
        hasher.update(chunk)

    uploaded_file.seek(0)  

    return hasher.hexdigest()

def slugify(value):
    if not value:
        return ""

    value = str(value).strip()

    value = unicodedata.normalize("NFKC", value)

    value = re.sub(r"[^\w\s\-ぁ-んァ-ン一-龯]", "", value)

    value = re.sub(r"[\s\-]+", "-", value)

    return value.strip("-").lower()






class MembershipPlanViewSet(viewsets.ModelViewSet):
    queryset = MembershipPlan.objects.all()
    serializer_class = MembershipPlanSerializer

    def perform_create(self, serializer):
        subdomain = self.request.data.get("club_subdomain")

        if not subdomain:
            raise serializers.ValidationError(
                {"club_subdomain": "This field is required."}
            )

        club = Club.objects.filter(
            subdomain=subdomain,
            is_deleted=False
        ).first()

        if not club:
            raise serializers.ValidationError(
                {"club_subdomain": "Club not found."}
            )

        if self.request.user != club.owner:
            raise serializers.ValidationError(
                {"detail": "Only owner can create plans."}
            )

        name = serializer.validated_data.get("name")
        price = serializer.validated_data.get("price")
        
        if not name:
            raise serializers.ValidationError({"name": "Name cannot be empty."})

        if price is None or price <= 0:
            raise serializers.ValidationError({"price": "Price must be greater than 0."})

        
        # 1️⃣ Save plan first
        plan = serializer.save(club=club)

        # 2️⃣ Create Stripe Product in the connected account
        product_data = {
            "name": plan.name,
            "metadata": {
                "club_id": club.id,
                "plan_id": plan.id
            },
        }
        
        if plan.description:
            product_data["description"] = plan.description
        
        product = stripe.Product.create(
            **product_data,
            stripe_account=club.stripe_account_id
        )

        # 3️⃣ Create Stripe Price
        stripe_price = stripe.Price.create(
            product=product.id,
            unit_amount=int(plan.price),  # convert to cents
            currency=plan.currency,
            recurring={"interval": plan.interval},
            stripe_account=club.stripe_account_id
        )

        # 4️⃣ Save Stripe price ID
        plan.stripe_product_id = product.id
        plan.stripe_price_id = stripe_price.id
        plan.save()
    
    def perform_update(self, serializer):
        plan = self.get_object()
        club = plan.club
    
        if self.request.user != club.owner:
            raise serializers.ValidationError({"detail": "Only owner can update plans."})
    
        old_price = plan.price
        old_name = plan.name
        old_description = plan.description
    
        updated_plan = serializer.save()
    
        stripe_account = club.stripe_account_id
    
        # 1️⃣ Update product name and description (safe)
        if updated_plan.stripe_product_id and (
            old_name != updated_plan.name or
            old_description != updated_plan.description
        ):
            stripe.Product.modify(
                updated_plan.stripe_product_id,
                name=updated_plan.name,
                description=updated_plan.description or "",
                stripe_account=stripe_account
            )
    
        # 2️⃣ If price changed → create NEW Stripe price
        if old_price != updated_plan.price:
            new_price = stripe.Price.create(
                product=updated_plan.stripe_product_id,
                unit_amount=int(updated_plan.price),
                currency=updated_plan.currency,
                recurring={"interval": updated_plan.interval},
                stripe_account=stripe_account
            )
    
            updated_plan.stripe_price_id = new_price.id
            updated_plan.save(update_fields=["stripe_price_id"])
    
    


class SlateImageViewSet(viewsets.ModelViewSet):
    queryset = SlateImage.objects.all()
    serializer_class = SlateImageSerializer

    def perform_create(self, serializer):
        serializer.save(
            club_id=self.request.data.get("club")
        )

    def create(self, request, *args, **kwargs):
        club_id = request.data.get("club")
        image_file = request.FILES.get("image")

        if not club_id or not image_file:
            return Response(
                {"detail": "club and image are required"},
                status=status.HTTP_400_BAD_REQUEST,
            )
 
        image_hash = hash_uploaded_file(image_file)
 
        existing = SlateImage.objects.filter(
            club_id=club_id,
            hash=image_hash,
        ).first()

        if existing:
            existing.created_at = timezone.now()
            existing.save(update_fields=["created_at"])
            
            serializer = self.get_serializer(existing)
            return Response(serializer.data, status=status.HTTP_200_OK)
 
        serializer = self.get_serializer(
            data={
                "image": image_file,
                "hash": image_hash,
            }
        )
        serializer.is_valid(raise_exception=True)
        self.perform_create(serializer)

        return Response(
            serializer.data,
            status=status.HTTP_201_CREATED,
        )





class JoinRequestViewSet(viewsets.ModelViewSet):
    queryset = JoinRequest.objects.all()
    serializer_class = JoinRequestSerializer

    def perform_create(self, serializer):
        subdomain = self.request.data.get("club_subdomain")
        is_family = self.request.data.get("is_family") == "true"
    
        club = Club.objects.filter(
            subdomain=subdomain,
            is_deleted=False
        ).first()
    
        if not club:
            raise serializers.ValidationError(
                {"club_subdomain": "Club not found."}
            )
    
        if not is_family:
            # Only restrict normal users
            existing = JoinRequest.objects.filter(
                user=self.request.user,
                club=club,
            ).first()
    
            if existing:
                raise serializers.ValidationError(
                    {"detail": "You already have a pending request."}
                )
    
        serializer.save(
            user=None if is_family else self.request.user,
            owner=self.request.user if is_family else None,
            club=club,
        )

    @action(detail=True, methods=["post"])
    def accept(self, request, pk=None):
        join_request = self.get_object()
    
        if request.user.id != join_request.club.owner_id:
            return Response({"detail": "Not allowed"}, status=403)
    
        # ---- Copy picture file ----
        new_picture = None
    
        if join_request.picture:
            original_file = join_request.picture
            original_file.open()
            file_content = original_file.read()
            original_file.close()
    
            # Create new filename
            base_name = os.path.basename(original_file.name)
            new_filename = f"{join_request.club.subdomain}/members/{base_name}"
    
            new_picture = ContentFile(file_content)
            new_picture.name = new_filename
    
        # ---- Create member ----
        member = Member.objects.create(
            club=join_request.club,
            user=join_request.user,
            owner=join_request.owner if join_request.owner else None,
            full_name=join_request.full_name,
            furigana=join_request.furigana,
            birth_date=join_request.birth_date,
            gender=join_request.gender,
            phone_number=join_request.phone_number,
            emergency_number=join_request.emergency_number,
            other_information=join_request.other_information,
            picture=new_picture,
            level=join_request.level or 1,
        )
    
        # ---- Delete join request AFTER copy ----
        join_request.delete()
    
        from .serializers import MemberSerializer
        return Response(
            MemberSerializer(member, context={"request": request}).data,
            status=status.HTTP_201_CREATED
        )
        
        
    @action(detail=True, methods=["post"])
    def reject(self, request, pk=None):
        join_request = self.get_object()
    
        if request.user.id != join_request.club.owner_id:
            return Response({"detail": "Not allowed"}, status=403)
    
        join_request.delete()
        return Response({"detail": "Deleted"}, status=status.HTTP_204_NO_CONTENT)
    
       
class MemberViewSet(viewsets.ModelViewSet):
    queryset = Member.objects.all()
    serializer_class = MemberSerializer

    @action(detail=True, methods=["post"])
    def freeze(self, request, pk=None):
        """Freeze / kyukai a member"""
        member = self.get_object()
        if not member.is_kyukai:
            member.is_kyukai = True
            member.kyukai_since = timezone.localdate()
            member.is_kyukai_paid = False
        else:
            member.is_kyukai = False
            member.kyukai_since = None
            member.is_kyukai_paid = False
        member.save()
        sync_member_quantity(member.club)

        status_text = "frozen" if member.is_kyukai else "unfrozen"
        return Response({
            "status": status_text,
            "is_kyukai": member.is_kyukai,
            "kyukai_since": member.kyukai_since,
            "is_kyukai_paid": member.is_kyukai_paid,
        }, status=status.HTTP_200_OK)

    @action(detail=True, methods=["delete"])
    def remove(self, request, pk=None):
        """Delete a member"""
        member = self.get_object()
        club = member.club

        member.delete()
        sync_member_quantity(club)
        return Response({"status": "deleted"}, status=status.HTTP_200_OK)

    def perform_create(self, serializer):
        subdomain = self.request.data.get("club_subdomain")

        club = Club.objects.filter(subdomain=subdomain, is_deleted=False).first()
        if not club:
            raise serializers.ValidationError({"club_subdomain": "Club not found."})
        
        existing_member = Member.objects.filter(club=club, user=self.request.user).first()


        member = serializer.save(club=club, user=self.request.user)
        sync_member_quantity(member.club)


class LessonViewSet(viewsets.ModelViewSet):
    queryset = Lesson.objects.all()
    serializer_class = LessonSerializer

    def perform_create(self, serializer):
        subdomain = self.request.data.get("club_subdomain")
        if not subdomain:
            raise serializers.ValidationError({"club_subdomain": "This field is required."})

        club = Club.objects.filter(subdomain=subdomain, is_deleted=False).first()
        if not club:
            raise serializers.ValidationError({"club_subdomain": "Club not found."})
        
        section_id = self.request.data.get("section_id")

        if section_id is None:
            raise serializers.ValidationError(
                {"section_id": "This field is required."}
            )
        instructor = None
        instructor_id = self.request.data.get("instructor_id")
        if instructor_id:
            try:
                instructor_id = int(instructor_id)
                instructor = Member.objects.filter(id=instructor_id, is_instructor=True).first()
            except ValueError:
                raise serializers.ValidationError({"instructor_id": "Invalid ID."})

            if not instructor:
                raise serializers.ValidationError({"instructor_id": "Instructor not found or not valid."})

        serializer.save(club=club, instructor=instructor, section_id=section_id)

class ParticipationViewSet(viewsets.ModelViewSet):
    queryset = Participation.objects.all()
    serializer_class = ParticipationSerializer

    @action(detail=False, methods=["post"], url_path="toggle-count")
    def toggle_count(self, request):
      with transaction.atomic():

        member_id = request.data.get("member")
        lesson_id = request.data.get("lesson")

        if not member_id or not lesson_id:
            return Response(
                {"detail": "member and lesson are required"},
                status=status.HTTP_400_BAD_REQUEST
            )

        member = Member.objects.filter(id=member_id).first()
        lesson = Lesson.objects.filter(id=lesson_id).first()

        if not member or not lesson:
            return Response(
                {"detail": "Member or lesson not found"},
                status=status.HTTP_404_NOT_FOUND
            )

        today = timezone.localtime(timezone.now()).date()
        yesterday = today - timedelta(days=1)
        level_key = str(member.level)

        participation = Participation.objects.filter(member=member, lesson=lesson).first()

        if participation:
            if participation.level_counts is None:
                participation.level_counts = {}

            if participation.last_participation_date == today:
                participation.total_count = max(participation.total_count - 1, 0)
                participation.monthly_count = max(participation.monthly_count - 1, 0)
                participation.level_counts[level_key] = max(
                    participation.level_counts.get(level_key, 0) - 1, 0
                )
                if participation.level_counts[level_key] == 0:
                    del participation.level_counts[level_key]
                participation.last_participation_date = participation.second_last_participation_date
            else:
                participation.total_count += 1
                participation.monthly_count += 1
                participation.level_counts[level_key] = participation.level_counts.get(level_key, 0) + 1
                if participation.second_last_participation_date != participation.last_participation_date:
                    participation.second_last_participation_date = participation.last_participation_date
                participation.last_participation_date = today

            participation.save()

            club = member.club
            try:
                milestones = club.level_milestones or {}
                if isinstance(milestones, str):
                    milestones = json.loads(milestones)
            except Exception:
                milestones = {}

            current_level_str = str(member.level)
            next_level = member.level + 1
            next_level_str = str(next_level)

            level_totals = get_level_participation(member)
            total_for_current_level = level_totals.get(member.level, 0)
            required = milestones.get(str(member.level))
            if required and total_for_current_level >= required:
                member.level += 1
                member.save()


        else:
            participation = Participation.objects.create(
                member=member,
                lesson=lesson,
                total_count=1,
                monthly_count=1,
                level_counts={level_key: 1},
                last_participation_date=today,
                second_last_participation_date=yesterday
            )
          

            club = member.club

            try:
                milestones = club.level_milestones or {}
                if isinstance(milestones, str):
                    milestones = json.loads(milestones)
            except Exception:
                milestones = {}

            current_level_str = str(member.level)
            next_level = member.level + 1
            next_level_str = str(next_level)

            level_totals = get_level_participation(member)
            total_for_current_level = level_totals.get(member.level, 0)

            required = milestones.get(current_level_str)

            if required and total_for_current_level >= required:
                member.level = next_level
                member.save()

        serializer = ParticipationSerializer(participation)

        response_data = serializer.data   
        response_data["member_data"] = {
            "milestones": milestones,
            "current_level": member.level,
            "current_count": total_for_current_level,
            "required_for_next_level": required,
            "level_totals": level_totals,
            "level_up": bool(required and total_for_current_level >= required),
        }
        return Response(response_data, status=status.HTTP_200_OK)


class ClubViewSet(viewsets.ModelViewSet):
    serializer_class = ClubSerializer
    queryset = Club.objects.all()

    def get_queryset(self):
        return (
            Club.objects.filter(is_deleted=False)
            .prefetch_related(

                Prefetch(
                    "members",
                    queryset=Member.objects.prefetch_related(
                        "participations",
                        Prefetch(
                            "subscriptions",
                            queryset=Subscription.objects.filter(status="active").prefetch_related("items__plan"),
                        ),
                    ),
                ),

                Prefetch(
                    "lessons",
                    queryset=Lesson.objects.prefetch_related("participations"),
                ),
                "slate_images",
                "join_requests",
                "membership_plans",
            )
        )

    @action(detail=False, methods=["get"], url_path="by-subdomain/(?P<subdomain>[^/.]+)")
    def by_subdomain(self, request, subdomain=None):
        club = self.get_queryset().filter(subdomain=subdomain).first()
        if not club:
            return Response({"detail": "Not found"}, status=status.HTTP_404_NOT_FOUND)
        serializer = self.get_serializer(club, context={"request": request})
        return Response(serializer.data)

    @action(detail=False, methods=["post"], url_path="create-trial")
    def create_trial(self, request):
        subdomain = request.data.get("subdomain")

        
        if not subdomain:
            return Response({"error": "サブドメインは必須項目です。"}, status=status.HTTP_400_BAD_REQUEST)

        if not VALID_SUBDOMAIN_RE.match(subdomain):
            return Response(
                {"error": "サブドメインは英数字とハイフンのみ使用可能です。"},
                status=status.HTTP_400_BAD_REQUEST
            )

        if subdomain.startswith("-") or subdomain.endswith("-"):
            return Response(
                {"error": "サブドメインはハイフンで始めたり終えたりできません。"},
                status=status.HTTP_400_BAD_REQUEST
            )

        if Club.objects.filter(subdomain=subdomain, is_deleted=False).exists():
            return Response(
                {"error": "このサブドメインはすでに使用されています。"},
                status=status.HTTP_400_BAD_REQUEST
            )

        FORBIDDEN_SUBDOMAINS = ["www", "kaibaru"]

        if subdomain.lower() in FORBIDDEN_SUBDOMAINS:
            return Response(
                {"error": f"サブドメイン '{subdomain}' は使用できません。"},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        club = Club.objects.create(subdomain=subdomain, owner=request.user, expiration_date = timezone.now())

        serializer = self.get_serializer(club, context={"request": request})
        return Response(serializer.data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["patch"], url_path="update_section_layout")
    def update_section_layout(self, request, pk=None):
        club = self.get_object()

        section_id = request.data.get("section_id")
        layout = request.data.get("layout")
    
        if not section_id or not isinstance(layout, dict):
            return Response(
                {"detail": "section_id and layout are required"},
                status=status.HTTP_400_BAD_REQUEST
            )
    
        try:
            page_content = json.loads(club.page_content)
        except Exception:
            return Response(
                {"detail": "Invalid page_content"},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )
    
        section = page_content.get("sections", {}).get(str(section_id))
    
        if not section:
            return Response(
                {"detail": "Section not found"},
                status=status.HTTP_404_NOT_FOUND
            )
    
        if section.get("type") != "custom":
            return Response(
                {"detail": "Only custom sections can have layouts"},
                status=status.HTTP_400_BAD_REQUEST
            )
    
        section["layout"] = layout
    
        club.page_content = json.dumps(page_content)
        club.save()
    
        serializer = self.get_serializer(club, context={"request": request})
        return Response(serializer.data, status=status.HTTP_200_OK)
    
    

    @action(detail=True, methods=["post"], url_path="mutate_page_section")
    def mutate_page_section(self, request, pk=None):
      with transaction.atomic():
        club = self.get_object()

        action_type = request.data.get("action")
        section_id = request.data.get("section")  # only for remove
        title = request.data.get("title", "")
        style = request.data.get("style")
        requested_order = request.data.get("order")
        section_type = request.data.get("type", "custom")
        icon = request.data.get("icon")

        ALLOWED_TYPES = {"custom", "schedule", "join", "member", "teacher", "slideshow", "header", "memberplans"}

        if action_type not in {"add", "remove", "edit", "add_slide", "remove_slide", "update_slide"}:
            return Response(
                {"detail": "Invalid action"},
                status=status.HTTP_400_BAD_REQUEST
            )

        if action_type == "add" and section_type not in ALLOWED_TYPES:
            return Response(
                {"detail": "Invalid section type"},
                status=status.HTTP_400_BAD_REQUEST
            )

        try:
            page_content = json.loads(club.page_content) if club.page_content else {
                "version": 1,
                "next_section_id": 1,
                "sections": {}
            }
        except json.JSONDecodeError:
            return Response(
                {"detail": "Invalid page_content JSON"},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

        sections = page_content.setdefault("sections", {})
        next_id = page_content.setdefault("next_section_id", 1)

        # Convert to list for ordering logic
        section_list = list(sections.values())

        # ----- RULE ENFORCEMENT -----

        has_join = any(
            s.get("type") == "join"
            for s in section_list
        )

        if action_type == "add" and section_type in {"member", "teacher"} and not has_join:
            return Response(
                {"detail": "Join section is required before adding this section"},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Prevent duplicate system sections

        UNIQUE_TYPES = {"join", "member", "teacher", "header", "memberplans"}
        if action_type == "add" and section_type in UNIQUE_TYPES:
            if any(s.get("type") == section_type for s in section_list):
                return Response(
                    {"detail": "This section type already exists"},
                    status=status.HTTP_400_BAD_REQUEST
                )

        # ----- ADD SECTION -----
        if action_type == "add":
            total_sections = len(section_list)

            try:
                order = int(requested_order)
            except (TypeError, ValueError):
                order = total_sections + 1

            order = max(1, min(order, total_sections + 1))

            # Shift existing sections down
            for section in section_list:
                if section["order"] >= order:
                    section["order"] += 1

            new_id = next_id
            page_content["next_section_id"] = new_id + 1

            base_slug = slugify(title or f"section-{new_id}")

            existing_slugs = {s.get("slug") for s in section_list if s.get("slug")}
            slug = base_slug
            i = 2
            while slug in existing_slugs:
                slug = f"{base_slug}-{i}"
                i += 1

            new_section = {
                "id": new_id,
                "order": order,
                "title": title or f"Section {new_id}",
                "type": section_type,
                "slug": slug,
            }
            if style:
                new_section["style"] = style
            if icon:
                new_section["icon"] = icon

            # Only custom sections get editable content
            if section_type == "custom":
                new_section["layout"] = {
                    "version": 2,
                    "boxes": []
                }

            if section_type == "header":
                header_data = request.data.get("header", {})

                new_section["header"] = {
                    "logo": header_data.get("logo"),
                    "logoHeight": header_data.get("logoHeight", 40),
                    "title": header_data.get("title", ""),
                    "subtitle": header_data.get("subtitle", ""),
                    "transparent": header_data.get("transparent", False),
                    "collapseAllNav": header_data.get("collapseAllNav", False),
                }

            if section_type == "slideshow":
                slides = request.data.get("slides")

                if isinstance(slides, list) and len(slides) >= 1:
                    new_section["slides"] = slides
                else:
                    new_section["slides"] = [
                        {
                            "id": str(uuid.uuid4()),
                            "image": None,
                            "heading": "New Slide",
                            "subheading": "",
                            "buttonText": "",
                            "buttonLink": "",
                            "buttonSectionId": None,
                            "buttonBoxId": None,
                            "textAlign": "center"
                        }
                    ]
            
            section_list.append(new_section)

        


        elif action_type == "edit":
            if not section_id or str(section_id) not in sections:
                return Response(
                    {"detail": "Section does not exist"},
                    status=status.HTTP_400_BAD_REQUEST
                )
        
            section = sections[str(section_id)]
            old_order = section["order"]

            if title and title != section.get("title"):
                old_slug = section.get("slug")
            
                # Build a set of all used slugs (current + previous) except this section
                existing_slugs = set()
                for s in section_list:
                    if s["id"] == section["id"]:
                        continue
                    if "slug" in s and s["slug"]:
                        existing_slugs.add(s["slug"])
                    if "previous_slugs" in s:
                        existing_slugs.update(s["previous_slugs"])
            
                # Generate a unique slug
                base_slug = slugify(title)
                slug = base_slug
                i = 2
                while slug in existing_slugs:
                    slug = f"{base_slug}-{i}"
                    i += 1
            
                # Save old slug in previous_slugs
                previous_slugs = section.setdefault("previous_slugs", [])
                if old_slug and old_slug != slug and old_slug not in previous_slugs:
                    previous_slugs.append(old_slug)
            
                section["slug"] = slug        

            # ---- title update ----
            if title:
                section["title"] = title
            
            if style is not None:
                section["style"] = style
            
            if "icon" in request.data:
                section["icon"] = icon

            if section.get("type") == "header" and "header" in request.data:
                header_data = request.data.get("header")

                if isinstance(header_data, dict):
                    section["header"] = {
                        "logo": header_data.get("logo"),
                        "logoHeight": header_data.get("logoHeight", 40),
                        "title": header_data.get("title", ""),
                        "subtitle": header_data.get("subtitle", ""),
                        "transparent": header_data.get("transparent", False),
                        "collapseAllNav": header_data.get("collapseAllNav", False),
                    }

            # ---- slideshow slides update ----
            if section.get("type") == "slideshow" and "slides" in request.data:
                slides = request.data.get("slides")
            
                if isinstance(slides, list) and len(slides) >= 1:
                    section["slides"] = slides
                else:
                    return Response(
                        {"detail": "Slideshow must contain at least one slide"},
                        status=status.HTTP_400_BAD_REQUEST
                    )
                    
             # ---- order update ----
            try:
                new_order = int(requested_order)
            except (TypeError, ValueError):
                new_order = old_order
                    
            total_sections = len(section_list)
            new_order = max(1, min(new_order, total_sections))
        
            if new_order != old_order:
                for s in section_list:
                    if s["id"] == section["id"]:
                        continue
        
                    if new_order > old_order:
                        # moving down
                        if old_order < s["order"] <= new_order:
                            s["order"] -= 1
                    else:
                        # moving up
                        if new_order <= s["order"] < old_order:
                            s["order"] += 1
        
                section["order"] = new_order
        
        
        # ----- ADD SLIDE -----
        elif action_type == "add_slide":
            if not section_id or str(section_id) not in sections:
                return Response(
                    {"detail": "Section does not exist"},
                    status=status.HTTP_400_BAD_REQUEST
                )

            section = sections[str(section_id)]

            if section.get("type") != "slideshow":
                return Response(
                    {"detail": "Not a slideshow section"},
                    status=status.HTTP_400_BAD_REQUEST
                )

            slides = section.setdefault("slides", [])

            slides.append({
                "id": str(uuid.uuid4()),
                "image": None,
                "heading": "New Slide",
                "subheading": "",
                "buttonText": "",
                "buttonLink": "",
                "textAlign": "center"
            })


        # ----- REMOVE SLIDE -----
        elif action_type == "remove_slide":
            slide_id = request.data.get("slide_id")

            if not section_id or str(section_id) not in sections:
                return Response(
                    {"detail": "Section does not exist"},
                    status=status.HTTP_400_BAD_REQUEST
                )

            section = sections[str(section_id)]

            if section.get("type") != "slideshow":
                return Response(
                    {"detail": "Not a slideshow section"},
                    status=status.HTTP_400_BAD_REQUEST
                )

            slides = section.get("slides", [])

            if len(slides) <= 1:
                return Response(
                    {"detail": "Slideshow must have at least one slide"},
                    status=status.HTTP_400_BAD_REQUEST
                )

            section["slides"] = [
                s for s in slides if str(s.get("id")) != str(slide_id)
            ]


        # ----- UPDATE SLIDE -----
        elif action_type == "update_slide":
            slide_id = request.data.get("slide_id")
            field = request.data.get("field")
            value = request.data.get("value")

            if not section_id or str(section_id) not in sections:
                return Response(
                    {"detail": "Section does not exist"},
                    status=status.HTTP_400_BAD_REQUEST
                )

            section = sections[str(section_id)]

            if section.get("type") != "slideshow":
                return Response(
                    {"detail": "Not a slideshow section"},
                    status=status.HTTP_400_BAD_REQUEST
                )

            valid_fields = {
                "heading",
                "subheading",
                "image",
                "buttonText",
                "buttonLink",
                "buttonSectionId",   # ADD
                "buttonBoxId",
                "textAlign",
            }

            if field not in valid_fields:
                return Response(
                    {"detail": "Invalid slide field"},
                    status=status.HTTP_400_BAD_REQUEST
                )

            for slide in section.get("slides", []):
                if str(slide.get("id")) == str(slide_id):
                    slide[field] = value

                    # ---- VALIDATION RULES ----

                    section_id_value = slide.get("buttonSectionId")
                    box_id_value = slide.get("buttonBoxId")
            
                    # Normalize empty values to None
                    if not section_id_value:
                        slide["buttonSectionId"] = None
                        slide["buttonBoxId"] = None
            
                    if slide.get("buttonBoxId") and not slide.get("buttonSectionId"):
                        return Response(
                            {"detail": "Box cannot be set without section"},
                            status=status.HTTP_400_BAD_REQUEST
                        )
                    break



        # ----- REMOVE SECTION -----
        elif action_type == "remove":
            if not section_id or str(section_id) not in sections:
                return Response(
                    {"detail": "Section does not exist"},
                    status=status.HTTP_400_BAD_REQUEST
                )

            removed_section = sections[str(section_id)]
            removed_order = removed_section["order"]

            # Prevent removing join if dependent sections exist
            if removed_section.get("type") == "join":
                if any(
                    s.get("type") in {"member", "teacher"}
                    for s in section_list
                ):
                    return Response(
                        {"detail": "Cannot remove join section while member or teacher sections exist"},
                        status=status.HTTP_400_BAD_REQUEST
                    )

            # Remove section
            section_list = [
                s for s in section_list if s["id"] != int(section_id)
            ]

            # Close ordering gap
            for section in section_list:
                if section["order"] > removed_order:
                    section["order"] -= 1

        # ----- REBUILD SECTIONS DICT -----
        sections.clear()
        for section in section_list:
            sections[str(section["id"])] = section

        club.page_content = json.dumps(page_content)
        club.save()

        serializer = self.get_serializer(club, context={"request": request})
        return Response(serializer.data, status=status.HTTP_200_OK)
